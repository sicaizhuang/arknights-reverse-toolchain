// Reconstructed IL2CPP labels; never presented as original C/C++ source.
//@category Arknights
import ghidra.app.script.GhidraScript;
import ghidra.program.model.symbol.SourceType;
import java.io.BufferedReader;
import java.io.FileReader;

public class ImportIl2CppSymbols extends GhidraScript {
    private String safe(String value) {
        String cleaned = value.replaceAll("[^A-Za-z0-9_.$<>]", "_");
        if (cleaned.length() > 220) cleaned = cleaned.substring(0, 220);
        return cleaned;
    }

    private String secondField(String line, int comma) {
        int start = comma + 1;
        if (start >= line.length()) return "";
        if (line.charAt(start) != '"') {
            int end = line.indexOf(',', start);
            return end < 0 ? line.substring(start) : line.substring(start, end);
        }
        StringBuilder value = new StringBuilder();
        for (int index = start + 1; index < line.length(); index++) {
            char current = line.charAt(index);
            if (current == '"') {
                if (index + 1 < line.length() && line.charAt(index + 1) == '"') {
                    value.append('"');
                    index++;
                    continue;
                }
                break;
            }
            value.append(current);
        }
        return value.toString();
    }

    private int importCsv(String path, String prefix) throws Exception {
        if (path == null || path.isEmpty()) return 0;
        int imported = 0;
        try (BufferedReader reader = new BufferedReader(new FileReader(path))) {
            String line = reader.readLine();
            while ((line = reader.readLine()) != null) {
                int comma = line.indexOf(',');
                if (comma <= 0) continue;
                String addressText = line.substring(0, comma).trim();
                String name = secondField(line, comma);
                try {
                    createLabel(toAddr(Long.decode(addressText)), prefix + safe(name), true, SourceType.IMPORTED);
                    imported++;
                } catch (Exception ignored) {
                    // Some runtime addresses are outside the imported image.
                }
            }
        }
        return imported;
    }

    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        int methods = args.length > 0 ? importCsv(args[0], "il2cpp_method_") : 0;
        int types = args.length > 1 ? importCsv(args[1], "il2cpp_type_") : 0;
        int strings = args.length > 2 ? importCsv(args[2], "il2cpp_string_") : 0;
        println("IL2CPP_SYMBOL_IMPORT methods=" + methods + " types=" + types + " strings=" + strings);
    }
}
