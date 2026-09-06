using System.Text.RegularExpressions;
using AnimeStudio;
using AnimeStudio.CLI;

if (args.Length < 2)
{
    Console.Error.WriteLine("usage: AnimeStudio.Phase2B <input> <output> --game <name> --dependency_map <name> [--group_assets <value>] [--export_type <value>] [--types <types...>]");
    return 2;
}

string? Option(string name)
{
    var index = Array.FindIndex(args, value => value.Equals(name, StringComparison.OrdinalIgnoreCase));
    return index >= 0 && index + 1 < args.Length ? args[index + 1] : null;
}

string[] MultiOption(string name)
{
    var index = Array.FindIndex(args, value => value.Equals(name, StringComparison.OrdinalIgnoreCase));
    if (index < 0)
        return [];
    var values = new List<string>();
    for (var position = index + 1; position < args.Length && !args[position].StartsWith("--", StringComparison.Ordinal); position++)
        values.Add(args[position]);
    return values.ToArray();
}

var input = new FileInfo(Path.GetFullPath(args[0]));
var output = new DirectoryInfo(Path.GetFullPath(args[1]));
var game = Option("--game") ?? "Arknights";
var mapName = Option("--dependency_map");
var types = MultiOption("--types");
var group = Enum.Parse<AssetGroupOption>(Option("--group_assets") ?? "ByType", ignoreCase: true);
var export = Enum.Parse<ExportType>(Option("--export_type") ?? "Convert", ignoreCase: true);
if (!input.Exists || string.IsNullOrWhiteSpace(mapName))
{
    Console.Error.WriteLine(!input.Exists ? $"input not found: {input.FullName}" : "--dependency_map is required");
    return 2;
}

Options MakeOptions(MapOpType operation) => new()
{
    Silent = false,
    LoggerFlags = [LoggerEvent.Debug, LoggerEvent.Info, LoggerEvent.Warning, LoggerEvent.Error],
    TypeFilter = types,
    NameFilter = Array.Empty<Regex>(),
    ContainerFilter = Array.Empty<Regex>(),
    GameName = game,
    MapOp = operation,
    MapType = ExportListType.JSON,
    MapName = mapName,
    UnityVersion = string.Empty,
    GroupAssetsType = group,
    AssetExportType = export,
    Key = 0,
    AIFile = null!,
    DummyDllFolder = null!,
    Input = input,
    Output = output,
};

try
{
    Console.WriteLine("[Phase2B] Loading existing dependency map in-process.");
    AnimeStudio.CLI.Program.Run(MakeOptions(MapOpType.CABMap));
    Console.WriteLine("[Phase2B] Exporting with dependency resolution retained in-process.");
    AnimeStudio.CLI.Program.Run(MakeOptions(MapOpType.Load));
    return 0;
}
catch (Exception exception)
{
    Console.Error.WriteLine(exception.ToString());
    return 4;
}
