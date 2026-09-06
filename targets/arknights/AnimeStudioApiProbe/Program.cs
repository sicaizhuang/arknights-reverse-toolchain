using System.Reflection;
using System.Runtime.Loader;
using System.Text.Json;

if (args.Length != 1)
{
    Console.Error.WriteLine("usage: AnimeStudioApiProbe <AnimeStudio.CLI.dll>");
    return 2;
}

var assemblyPath = Path.GetFullPath(args[0]);
var assemblyDirectory = Path.GetDirectoryName(assemblyPath)!;
var context = new AssemblyLoadContext("animestudio-probe", isCollectible: true);
context.Resolving += (_, name) =>
{
    var candidate = Path.Combine(assemblyDirectory, name.Name + ".dll");
    return File.Exists(candidate) ? context.LoadFromAssemblyPath(candidate) : null;
};

try
{
    var assembly = context.LoadFromAssemblyPath(assemblyPath);
    var enumType = assembly.GetType("AnimeStudio.CLI.MapOpType", throwOnError: true)!;
    var values = Enum.GetNames(enumType).ToDictionary(
        name => name,
        name => Convert.ToInt64(Enum.Parse(enumType, name)));
    var payload = new
    {
        status = "completed",
        assembly_path = assemblyPath,
        assembly_name = assembly.GetName().Name,
        assembly_version = assembly.GetName().Version?.ToString(),
        map_op_values = values,
    };
    Console.WriteLine(JsonSerializer.Serialize(payload));
    return 0;
}
catch (Exception exception)
{
    Console.Error.WriteLine(exception.ToString());
    return 4;
}
finally
{
    context.Unload();
}
