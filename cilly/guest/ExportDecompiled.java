// Writes decompiled C for every function to <outdir>/<program name>.c (cilly's ghidra-decompile).
// Runs once per imported program, so a fat binary that imports as several leaves one file each.
//@category cilly
import java.io.File;
import java.io.FileWriter;
import java.io.PrintWriter;

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.Function;

public class ExportDecompiled extends GhidraScript {
	@Override
	public void run() throws Exception {
		DecompInterface decompiler = new DecompInterface();
		try {
			if (!decompiler.openProgram(currentProgram)) {
				println("ghidra-decompile: couldn't open " + currentProgram.getName() + " to decompile");
				return;  // no file written, so ghidra-decompile reports that nothing was produced
			}
			File out = new File(getScriptArgs()[0], currentProgram.getName() + ".c");
			int done = 0, failed = 0;
			try (PrintWriter w = new PrintWriter(new FileWriter(out))) {
				for (Function f : currentProgram.getFunctionManager().getFunctions(true)) {
					if (monitor.isCancelled()) {
						break;
					}
					DecompileResults r = decompiler.decompileFunction(f, 120, monitor);
					if (r != null && r.decompileCompleted()) {
						w.println(r.getDecompiledFunction().getC());
						done++;
					}
					else {
						w.println("/* " + f.getName() + " at " + f.getEntryPoint() + " wasn't decompiled: " +
							(r == null ? "no result" : r.getErrorMessage()) + " */\n");
						failed++;
					}
				}
			}
			if (done == 0) {
				out.delete();  // nothing decompiled: leave no empty file, so the wrapper can tell
				println("ghidra-decompile: no functions decompiled in " + currentProgram.getName());
			}
			else {
				println("ghidra-decompile: " + done + " functions in " + out +
					(failed > 0 ? ", " + failed + " not decompiled" : ""));
			}
		}
		finally {
			decompiler.dispose();
		}
	}
}
