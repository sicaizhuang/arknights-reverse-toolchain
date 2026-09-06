//@category Arknights
// Focused evidence extractor; output is reconstructed decompiler text, not source.
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.Data;
import ghidra.program.model.listing.DataIterator;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionIterator;
import ghidra.program.model.symbol.Reference;
import java.io.PrintWriter;
import java.io.File;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;

public class FocusKeywordFunctions extends GhidraScript {
    private static final String[] KEYWORDS = {
        "operator", "animation", "attack", "summon", "projectile",
        "damage", "wisadel", "voice", "audio", "soul"
    };

    private boolean matches(String text) {
        String lowered = text.toLowerCase(Locale.ROOT);
        for (String keyword : KEYWORDS) if (lowered.contains(keyword)) return true;
        return false;
    }

    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length == 0) throw new IllegalArgumentException("output path is required");
        int maxFunctions = args.length > 1 ? Integer.parseInt(args[1]) : 64;
        File output = new File(args[0]);
        output.getParentFile().mkdirs();
        LinkedHashMap<String, Function> functions = new LinkedHashMap<>();
        int matchedStrings = 0;
        try (PrintWriter writer = new PrintWriter(output, "UTF-8")) {
            DataIterator dataIterator = currentProgram.getListing().getDefinedData(true);
            while (dataIterator.hasNext()) {
                Data data = dataIterator.next();
                Object value = data.getValue();
                if (value == null) continue;
                String text = value.toString();
                if (!matches(text)) continue;
                matchedStrings++;
                writer.println("FOCUS_STRING " + data.getAddress() + " " + text.replace('\n', ' '));
                Reference[] references = getReferencesTo(data.getAddress());
                for (Reference reference : references) {
                    Function function = currentProgram.getFunctionManager().getFunctionContaining(reference.getFromAddress());
                    if (function != null) functions.put(function.getEntryPoint().toString(), function);
                }
            }
            writer.println("FOCUS_COUNTS strings=" + matchedStrings + " functions=" + functions.size());
            DecompInterface decompiler = new DecompInterface();
            decompiler.openProgram(currentProgram);
            int emitted = 0;
            for (Map.Entry<String, Function> entry : functions.entrySet()) {
                if (emitted++ >= maxFunctions) break;
                Function function = entry.getValue();
                writer.println("\nFOCUS_FUNCTION " + function.getEntryPoint() + " " + function.getName());
                DecompileResults result = decompiler.decompileFunction(function, 30, monitor);
                if (result.decompileCompleted() && result.getDecompiledFunction() != null) {
                    writer.println(result.getDecompiledFunction().getC());
                } else {
                    writer.println("DECOMPILE_FAILED " + result.getErrorMessage());
                }
            }
            decompiler.dispose();
        }
        println("IL2CPP_FOCUS strings=" + matchedStrings + " functions=" + functions.size() + " emitted=" + Math.min(functions.size(), maxFunctions));
    }
}
