#!/usr/bin/python3
"""jar-diff <old.jar> <new.jar> [outdir]: what changed between two versions of a jar (outdir defaults to ~/scratch).

Unpacks both jars, decompiles both with Vineflower (CFR for any class Vineflower fails on, when it's
installed), and prints added, removed and changed classes and resource files.  YAML, JSON and
.properties files get a key by key diff, so renamed or removed message keys stand out.  Everything
stays on disk under outdir for a closer look:

    outdir/old/files, outdir/new/files   the unpacked jars
    outdir/old/src, outdir/new/src       the decompiled sources
    outdir/diff/                         a unified diff for each changed class and resource file
    outdir/summary.txt                   what was printed

Part of the cilly decompilers toolchain.  Jars may be obfuscated or hostile.  A decompiler, given the
original jar, would follow a ../ or absolute entry name (or a class whose internal name holds ../) and
write outside the output folder, which runs as the agent.  So it is only ever handed a jar rebuilt from
the safely unpacked .class files whose internal name is their path, and its output is copied back through
safe paths: nothing lands outside outdir, odd names are skipped with a note, and a decompiler that crashes
only costs its classes.
"""

import difflib
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile

VINEFLOWER = "/opt/decompilers/vineflower.jar"
CFR = "/opt/decompilers/cfr.jar"
VF_FAILED = "$VF: Couldn't be decompiled"
CFR_FAILED = "This method has failed to decompile"
TEXT_KEYS = (".yml", ".yaml", ".json", ".properties")
MAX_ENTRIES = 200_000
MAX_BYTES = 4 << 30       # unpacked, per jar
MAX_TEXT = 4 << 20        # files larger than this get no text diff
MAX_KEYS = 100_000        # flattened keys per file (YAML aliases can multiply them)
MAX_CLASS_HEADER = 16 << 20  # read this much of a .class file to find its name
CP_SIZES = {3: 4, 4: 4, 5: 8, 6: 8, 8: 2, 9: 4, 10: 4, 11: 4, 12: 4, 15: 3, 16: 2, 17: 4, 18: 4, 19: 2, 20: 2}
SHOWN = 60                # names listed per group before "and N more"
TIMEOUT = 1800            # seconds per decompiler run; JAR_DIFF_TIMEOUT overrides it (checked in main)


def _env_timeout() -> int:
    v = os.environ.get("JAR_DIFF_TIMEOUT")
    if v is None:
        return TIMEOUT
    try:
        n = int(v)
        if n <= 0:
            raise ValueError
    except ValueError:
        print(f"jar-diff: JAR_DIFF_TIMEOUT must be a positive whole number of seconds, not {v!r}", file=sys.stderr)
        raise SystemExit(2)
    return n


def safe_name(name: str):
    """An entry's path inside the jar, or None when it could leave the folder or isn't a plain path."""
    if "\0" in name or "\\" in name or name.startswith("/"):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or len(p.encode()) > 255 for p in parts):
        return None
    return "/".join(parts)


def unpack(jar: str, dest: str, notes: list) -> dict:
    """Every file in the jar under dest: name -> sha256.  Skips (with a note) what can't be written safely."""
    out, total = {}, 0
    with zipfile.ZipFile(jar) as z:
        infos = z.infolist()
        if len(infos) > MAX_ENTRIES:
            notes.append(f"{os.path.basename(jar)}: only the first {MAX_ENTRIES} of {len(infos)} entries were unpacked")
        for info in infos[:MAX_ENTRIES]:
            if info.is_dir():
                continue
            name = safe_name(info.filename)
            if name is None:
                notes.append(f"{os.path.basename(jar)}: skipped an entry with an unsafe name: {info.filename[:200]!r}")
                continue
            if name in out:
                notes.append(f"{os.path.basename(jar)}: {name} is in the jar twice, so the first one is used")
                continue
            total += info.file_size
            if total > MAX_BYTES:
                notes.append(f"{os.path.basename(jar)}: stopped unpacking past {MAX_BYTES >> 30} GiB")
                break
            path = os.path.join(dest, name)
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                h = hashlib.sha256()
                with z.open(info) as src, open(path, "xb") as dst:
                    for chunk in iter(lambda: src.read(1 << 20), b""):
                        h.update(chunk)
                        dst.write(chunk)
                out[name] = h.hexdigest()
            except (OSError, zipfile.BadZipFile, NotImplementedError, RuntimeError) as e:
                notes.append(f"{os.path.basename(jar)}: couldn't unpack {name}: {e.__class__.__name__}")
    return out


def class_name(path: str):
    """The internal name a class file declares (this_class), or None when it isn't a class file."""
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_CLASS_HEADER)
        if data[:4] != b"\xca\xfe\xba\xbe":
            return None
        count, = struct.unpack_from(">H", data, 8)
        pool, i, pos = {}, 1, 10
        while i < count:
            tag = data[pos]
            if tag == 1:
                n, = struct.unpack_from(">H", data, pos + 1)
                pool[i] = data[pos + 3:pos + 3 + n]
                pos += 3 + n
            elif tag == 7:
                pool[i], = struct.unpack_from(">H", data, pos + 1)
                pos += 3
            else:
                pos += 1 + CP_SIZES[tag]
            i += 2 if tag in (5, 6) else 1  # long and double take two slots
        this, = struct.unpack_from(">H", data, pos + 2)
        name = pool[pool[this]]
        return name.decode() if pos <= len(data) and isinstance(name, bytes) else None
    except (OSError, struct.error, IndexError, KeyError, TypeError, UnicodeDecodeError):
        return None


def clean_class_jar(files_dir: str, dest: str, notes: list) -> set:
    """A jar with only the .class files under files_dir (already unpacked with safe names) that declare
    their own path as their name.  A decompiler names its output after that, so run on this jar it has
    no ../ or absolute name to follow out of its output folder.  Returns the paths it took."""
    took = set()
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as out:
        for dirpath, _, names in os.walk(files_dir):
            for n in names:
                full = os.path.join(dirpath, n)
                rel = os.path.relpath(full, files_dir)  # relpath of a walked file has no ..
                if not n.endswith(".class") or rel.startswith("META-INF/versions/"):
                    continue
                if class_name(full) != rel[:-len(".class")]:
                    notes.append(f"{rel}: not decompiled, it isn't a class file or the class in it has another name")
                    continue
                out.write(full, rel)
                took.add(rel)
    return took


def harvest(frm: str, to: str, notes: list) -> int:
    """Copy a decompiler's output tree from frm into to.  os.walk never yields a path that leaves to."""
    n = 0
    for dirpath, _, names in os.walk(frm):
        for name in names:
            rel = os.path.relpath(os.path.join(dirpath, name), frm)
            dst = os.path.join(to, rel)
            try:
                os.makedirs(os.path.dirname(dst) or to, exist_ok=True)
                shutil.copyfile(os.path.join(dirpath, name), dst)
                n += 1
            except OSError as e:
                notes.append(f"couldn't write {rel}: {e.__class__.__name__}")
    return n


def take_key(frm: str, src: str, k: str) -> str:
    """Move one class's source frm/<k>.java to src/<k>.java, by its safe key.  Returns the text, or ""."""
    alt = os.path.join(frm, k + ".java")
    if not os.path.isfile(alt):
        return ""
    dst = os.path.join(src, k + ".java")
    os.makedirs(os.path.dirname(dst) or src, exist_ok=True)
    shutil.copyfile(alt, dst)
    return read(dst)


def class_key(name: str):
    """The top-level class a .class file belongs to (inner classes go with their outer class)."""
    if not name.endswith(".class") or name.startswith("META-INF/versions/") or name.endswith("module-info.class"):
        return None
    base = name[:-len(".class")]
    head, _, last = base.rpartition("/")
    last = last.split("$", 1)[0] or last
    return f"{head}/{last}" if head else last


def run(argv: list, notes: list, what: str) -> None:
    try:
        r = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           timeout=TIMEOUT)
        if r.returncode:
            err = r.stderr.decode(errors="replace").strip().splitlines()
            notes.append(f"{what} exited with {r.returncode}" + (f": {err[-1][:200]}" if err else ""))
    except (OSError, subprocess.TimeoutExpired) as e:
        notes.append(f"{what} didn't finish: {e.__class__.__name__}")


def _rm(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def cfr_batches(keys, budget: int = 60000):
    """CFR's --jarfilter is one argument; split the class list so the regex stays well under the limit."""
    batch, size = [], 0
    for k in sorted(keys):
        piece = len(k) * 2 + 2
        if batch and size + piece > budget:
            yield batch
            batch, size = [], 0
        batch.append(k)
        size += piece
    if batch:
        yield batch


def read(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return f.read(MAX_TEXT + 1).decode("utf-8", errors="replace")
    except OSError:
        return ""


def decompile(clean: str, src: str, keys: set, notes: list, name: str, vineflower=None, cfr=None) -> list:
    """Sources for every class in keys under src, from a clean jar (only safe .class entries).  The
    decompiler writes into a private temp dir; only the known keys are copied into src.  Returns the
    classes CFR stood in for."""
    vineflower = vineflower or ["java", "-jar", VINEFLOWER, "--folder", "-s"]
    cfr = cfr or (["java", "-jar", CFR] if os.path.exists(CFR) else None)
    work = os.path.dirname(src)  # in the output folder, as /tmp may be in memory
    with tempfile.TemporaryDirectory(dir=work, prefix=".decompiling-") as tmp:
        vf_out = os.path.join(tmp, "vf")
        os.mkdir(vf_out)
        run(vineflower + [clean, vf_out], notes, f"Vineflower on {name}")
        for k in keys:
            take_key(vf_out, src, k)
    failed = sorted(k for k in keys if VF_FAILED in read(os.path.join(src, k + ".java")) or not os.path.isfile(os.path.join(src, k + ".java")))
    if not failed or not cfr:
        if failed:
            notes.append(f"{name}: Vineflower couldn't decompile {len(failed)} class(es), and CFR isn't installed")
        return []
    with tempfile.TemporaryDirectory(dir=work, prefix=".decompiling-") as tmp:
        cfr_out = os.path.join(tmp, "cfr")
        os.mkdir(cfr_out)
        for batch in cfr_batches(failed):
            filt = ["--jarfilter", "^(" + "|".join(re.escape(k.replace("/", ".")) for k in batch) + ")$"]
            run(cfr + [clean, "--outputdir", cfr_out, "--silent", "true"] + filt, notes, f"CFR on {name}")
        used = []
        for k in failed:
            if CFR_FAILED not in read(os.path.join(cfr_out, k + ".java")) and take_key(cfr_out, src, k):
                used.append(k)
        left = len(failed) - len(used)
        if left:
            notes.append(f"{name}: {left} class(es) couldn't be fully decompiled by either decompiler (marked in the source)")
        return used


def size(obj, budget: list) -> None:
    """Counts every value, so YAML aliases that multiply a document stop at MAX_KEYS."""
    budget[0] -= 1
    if budget[0] < 0:
        raise ValueError("too many values")
    for v in obj.values() if isinstance(obj, dict) else obj if isinstance(obj, list) else ():
        size(v, budget)


def flatten(obj, prefix="", out=None) -> dict:
    """Nested mappings as dotted keys: {"a": {"b": 1}} -> {"a.b": "1"}.  Lists are one value."""
    if out is None:
        size(obj, [MAX_KEYS])
        out = {}
    if isinstance(obj, dict) and obj:
        for k, v in obj.items():
            flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    else:
        out[prefix] = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    return out


def parse_properties(text: str) -> dict:
    out, lines = {}, text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].lstrip()
        i += 1
        if not line or line[0] in "#!":
            continue
        while (len(line) - len(line.rstrip("\\"))) % 2 == 1 and i < len(lines):  # odd trailing backslashes
            line = line[:-1] + lines[i].lstrip()
            i += 1
        m = re.match(r"((?:\\.|[^=:\s\\])*)\s*[=:\s]?\s*(.*)", line)
        key = re.sub(r"\\(.)", r"\1", m.group(1))
        out[key] = m.group(2)
    return out


def keys_of(name: str, text: str) -> dict:
    if name.endswith(".properties"):
        return parse_properties(text)
    if name.endswith(".json"):
        return flatten(json.loads(text))
    import yaml  # python3-yaml, installed with the toolchain
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    return flatten(yaml.load(text, Loader=loader))


def short(v: str, n: int = 80) -> str:
    v = " ".join(str(v).split())
    return json.dumps(v if len(v) <= n else v[:n - 1] + "…", ensure_ascii=False)


def key_diff(old: dict, new: dict) -> list:
    """Lines for a key by key diff.  A removed key whose value shows up under exactly one added key is a rename."""
    gone = [k for k in old if k not in new]
    came = [k for k in new if k not in old]
    by_value = {}
    for k in came:
        by_value.setdefault(new[k], []).append(k)
    lines, renamed_to, renamed_from = [], set(), set()
    for k in gone:
        match = [c for c in by_value.get(old[k], []) if c not in renamed_to]
        if len(match) == 1 and old[k] not in ("", "null", "[]", "{}"):
            renamed_to.add(match[0])
            renamed_from.add(k)
            lines.append(f"  renamed  {k}  ->  {match[0]}")
    lines += [f"  removed  {k} = {short(old[k])}" for k in gone if k not in renamed_from]
    lines += [f"  added    {k} = {short(new[k])}" for k in came if k not in renamed_to]
    lines += [f"  changed  {k}: {short(old[k])} -> {short(new[k])}" for k in old if k in new and old[k] != new[k]]
    return lines


def write_diff(path: str, a: str, b: str, name: str) -> bool:
    d = list(difflib.unified_diff(a.splitlines(keepends=True), b.splitlines(keepends=True), f"old/{name}", f"new/{name}"))
    if not d:
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(ln if ln.endswith("\n") else ln + "\n" for ln in d)
    return True


def escaped(s: str) -> str:
    """Text for summary.txt and the terminal: control characters (C0 and C1, so ESC and CSI too) and line
    separators escaped, so a crafted name, key or message can't forge a line or drive the terminal."""
    return re.sub(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]",
                  lambda m: f"\\x{ord(m.group()):02x}" if m.group() < "\u0100" else f"\\u{ord(m.group()):04x}", s)


def listed(title: str, names: list) -> list:
    if not names:
        return []
    out = [f"{title} ({len(names)}):"] + [f"  {n}" for n in names[:SHOWN]]
    if len(names) > SHOWN:
        out.append(f"  and {len(names) - SHOWN} more")
    return out


def compare(old_jar: str, new_jar: str, outdir: str, vineflower=None, cfr=None) -> list:
    notes, side = [], {}
    for tag, jar in (("old", old_jar), ("new", new_jar)):
        os.makedirs(os.path.join(outdir, tag, "files"), exist_ok=True)
        files = unpack(jar, os.path.join(outdir, tag, "files"), notes)
        classes = {}
        for n, h in files.items():
            k = class_key(n)
            if k:
                classes.setdefault(k, []).append((n, h))
        clean = os.path.join(outdir, tag, "classes.jar")
        took = clean_class_jar(os.path.join(outdir, tag, "files"), clean, notes)
        keys = {class_key(n) for n in took} - {None}
        cfr_used = decompile(clean, os.path.join(outdir, tag, "src"), keys, notes, os.path.basename(jar), vineflower, cfr)
        _rm(clean)
        side[tag] = {"files": files, "classes": {k: sorted(v) for k, v in classes.items()}, "cfr": cfr_used}
    old, new = side["old"], side["new"]
    out = [f"jar-diff {os.path.basename(old_jar)} -> {os.path.basename(new_jar)}", ""]

    oc, nc = old["classes"], new["classes"]
    changed, recompiled = [], []
    for k in sorted(set(oc) & set(nc)):
        if oc[k] == nc[k]:
            continue
        a = read(os.path.join(outdir, "old", "src", k + ".java"))
        b = read(os.path.join(outdir, "new", "src", k + ".java"))
        if write_diff(os.path.join(outdir, "diff", "classes", k + ".java.diff"), a, b, k + ".java") or not (a or b):
            changed.append(k)  # with no source on either side (see the notes), there's no diff to show
        else:
            recompiled.append(k)
    out += listed("Added classes", sorted(set(nc) - set(oc)))
    out += listed("Removed classes", sorted(set(oc) - set(nc)))
    out += listed("Changed classes (diffs in diff/classes/)", changed)
    out += listed("Recompiled, same source", recompiled)

    of = {n: h for n, h in old["files"].items() if not n.endswith(".class")}
    nf = {n: h for n, h in new["files"].items() if not n.endswith(".class")}
    out += listed("Added files", sorted(set(nf) - set(of)))
    out += listed("Removed files", sorted(set(of) - set(nf)))
    res = sorted(n for n in set(of) & set(nf) if of[n] != nf[n])
    out += listed("Changed files (diffs in diff/files/)", res)
    for n in res:
        a, b = read(os.path.join(outdir, "old", "files", n)), read(os.path.join(outdir, "new", "files", n))
        if "\x00" in a or "\x00" in b:
            out += ["", f"{n}: binary (has NUL bytes), not diffed"]
            continue
        write_diff(os.path.join(outdir, "diff", "files", n + ".diff"), a, b, n)
        if not n.lower().endswith(TEXT_KEYS) or len(a) > MAX_TEXT or len(b) > MAX_TEXT:
            continue
        try:
            lines = key_diff(keys_of(n.lower(), a), keys_of(n.lower(), b))
        except Exception as e:  # noqa: BLE001 - a file that doesn't parse still has its text diff
            out += ["", f"{n}: not parsed ({e.__class__.__name__}), see its text diff"]
            continue
        out += ["", f"{n}, key by key:"] + (lines or ["  only formatting or comments changed"])

    for tag in ("old", "new"):
        if side[tag]["cfr"]:
            out += ["", f"CFR stood in for Vineflower on {len(side[tag]['cfr'])} {tag} class(es):"] + [f"  {k}" for k in side[tag]["cfr"][:SHOWN]]
    if notes:
        out += ["", "Notes:"] + [f"  {n}" for n in notes]
    if len(out) == 2:
        out.append("No differences.")
    out += ["", f"Everything is in {outdir}"]
    return [escaped(ln) for ln in out]  # names, keys, values and notes all come from the jars


def split_options(tool: str, args: list):
    """Options and paths, in any order.  CFR's options take a value (--silent true), Vineflower's don't (-dgs=1)."""
    opts, paths, i = [], [], 0
    while i < len(args):
        if args[i].startswith("-") and len(args[i]) > 1:
            take = 2 if tool == "cfr" else 1
            opts += args[i:i + take]
            i += take
        else:
            paths.append(args[i])
            i += 1
    return opts, paths


def decompile_cli(args: list, vineflower=None, cfr=None) -> int:
    """`jar-diff --decompile vineflower|cfr [options] <jar or class> <outdir> [options]`: what the vineflower
    and cfr wrappers run, so they decompile a clean jar and copy the result back through safe paths."""
    tool = args[0] if args else ""
    opts, paths = split_options(tool, args[1:])
    if tool not in ("vineflower", "cfr") or len(paths) != 2:
        print(f"usage: {tool or 'vineflower|cfr'} [options] <jar or class> <outdir>", file=sys.stderr)
        return 2
    global TIMEOUT
    TIMEOUT = _env_timeout()
    src_in, outdir = os.path.abspath(paths[0]), os.path.abspath(paths[1])
    if not os.path.isfile(src_in):
        print(f"{tool}: {escaped(paths[0])} isn't a file", file=sys.stderr)
        return 1
    notes = []
    os.makedirs(outdir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=outdir, prefix=".decompiling-") as tmp:  # not /tmp, which may be in memory
        files_dir = os.path.join(tmp, "in")
        os.makedirs(files_dir)
        name = class_name(src_in) if src_in.endswith(".class") else None
        try:
            if zipfile.is_zipfile(src_in):
                unpack(src_in, files_dir, notes)
            elif name and safe_name(name + ".class") == name + ".class":
                os.makedirs(os.path.dirname(os.path.join(files_dir, name)), exist_ok=True)
                shutil.copyfile(src_in, os.path.join(files_dir, name + ".class"))  # where its name says it belongs
            else:
                print(f"{tool}: {escaped(paths[0])} isn't a jar or a class file", file=sys.stderr)
                return 1
        except (OSError, zipfile.BadZipFile) as e:
            print(f"{tool}: couldn't read {escaped(paths[0])}: {e.__class__.__name__}", file=sys.stderr)
            return 1
        clean = os.path.join(tmp, "classes.jar")
        clean_class_jar(files_dir, clean, notes)
        out_tmp = os.path.join(tmp, "out")
        os.mkdir(out_tmp)
        if tool == "vineflower":
            argv = (vineflower or ["java", "-jar", VINEFLOWER, "--folder", "-s"]) + opts + [clean, out_tmp]
        else:
            argv = (cfr or ["java", "-jar", CFR]) + [clean, "--outputdir", out_tmp] + opts
        run(argv, notes, f"{tool} on {os.path.basename(src_in)}")
        n = harvest(out_tmp, outdir, notes)
    for note in notes:
        print(escaped(note), file=sys.stderr)
    return 0 if n else 1


def main(argv: list, vineflower=None, cfr=None) -> int:
    if argv and argv[0] == "--decompile":
        return decompile_cli(argv[1:], vineflower, cfr)
    if len(argv) not in (2, 3) or any(a in ("-h", "--help") for a in argv):
        print(__doc__.split("\n\n")[0], file=sys.stderr)
        return 2
    global TIMEOUT
    TIMEOUT = _env_timeout()
    old_jar, new_jar = os.path.abspath(argv[0]), os.path.abspath(argv[1])  # absolute, so a name with a leading - is a path
    outdir = os.path.abspath(argv[2]) if len(argv) == 3 else os.path.join(
        os.path.expanduser("~/scratch"), f"jar-diff-{os.path.basename(old_jar)[:-4]}-vs-{os.path.basename(new_jar)[:-4]}")
    for j in (old_jar, new_jar):
        if not zipfile.is_zipfile(j):
            print(f"jar-diff: {j} isn't a jar", file=sys.stderr)
            return 1
    if os.path.exists(outdir) and os.listdir(outdir):
        print(f"jar-diff: {outdir} isn't empty. Pick another folder.", file=sys.stderr)
        return 1
    os.makedirs(outdir, exist_ok=True)
    lines = compare(old_jar, new_jar, outdir, vineflower, cfr)
    text = "\n".join(lines) + "\n"
    with open(os.path.join(outdir, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
