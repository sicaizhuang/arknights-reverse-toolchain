using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using UnityEditor;
using UnityEngine;

public static class ArknightsEffectRenderer
{
    [Serializable]
    private sealed class RenderInput
    {
        public string main_bundle;
        public string[] dependency_bundles;
        public string asset_path;
        public float sample_time;
        public string output_image;
        public string output_report;
    }

    public static void Run()
    {
        int exitCode = 1;
        RenderInput input = null;
        var loaded = new List<AssetBundle>();
        var loadedPaths = new List<string>();
        var failedPaths = new List<string>();
        var materialFacts = new List<string>();
        string error = null;
        string status = "failed";
        int assetCount = 0;
        int rendererCount = 0;
        int particleCount = 0;
        int unsupportedShaderCount = 0;
        int nullMaterialSlotCount = 0;
        int visiblePixelCount = 0;
        RectInt visibleBounds = new RectInt();
        Bounds worldBounds = new Bounds(Vector3.zero, Vector3.zero);

        try
        {
            string inputPath = GetArgument("-stageInput");
            if (string.IsNullOrWhiteSpace(inputPath) || !File.Exists(inputPath))
                throw new FileNotFoundException("Missing -stageInput JSON.", inputPath);
            input = JsonUtility.FromJson<RenderInput>(File.ReadAllText(inputPath, Encoding.UTF8));
            if (input == null || string.IsNullOrWhiteSpace(input.main_bundle) || string.IsNullOrWhiteSpace(input.asset_path))
                throw new InvalidDataException("Invalid effect stage input.");

            foreach (string path in input.dependency_bundles ?? Array.Empty<string>())
            {
                AssetBundle dependency = AssetBundle.LoadFromFile(path);
                if (dependency == null)
                {
                    failedPaths.Add(path);
                    continue;
                }
                loaded.Add(dependency);
                loadedPaths.Add(path);
            }
            if (failedPaths.Count > 0)
                throw new InvalidOperationException("One or more dependency Bundles failed to load.");

            AssetBundle main = AssetBundle.LoadFromFile(input.main_bundle);
            if (main == null)
                throw new InvalidOperationException("The converted main Bundle failed to load.");
            loaded.Add(main);
            loadedPaths.Add(input.main_bundle);

            string[] assets = main.GetAllAssetNames();
            assetCount = assets.Length;
            string canonicalAsset = assets.FirstOrDefault(item => string.Equals(item, input.asset_path, StringComparison.OrdinalIgnoreCase));
            if (canonicalAsset == null)
                throw new InvalidOperationException("Requested effect asset is absent from the main Bundle.");
            GameObject prefab = main.LoadAsset<GameObject>(canonicalAsset);
            if (prefab == null)
                throw new InvalidOperationException("Requested asset did not load as a GameObject prefab.");

            GameObject instance = UnityEngine.Object.Instantiate(prefab);
            instance.name = "ArknightsEffectRendererInstance";
            instance.SetActive(true);

            foreach (Animator animator in instance.GetComponentsInChildren<Animator>(true))
            {
                animator.enabled = true;
                animator.Rebind();
                animator.Update(input.sample_time);
            }
            foreach (Animation animation in instance.GetComponentsInChildren<Animation>(true))
            {
                animation.enabled = true;
                if (animation.clip == null)
                    continue;
                animation.Play(animation.clip.name);
                animation[animation.clip.name].time = Math.Min(input.sample_time, animation.clip.length);
                animation.Sample();
            }
            ParticleSystem[] particles = instance.GetComponentsInChildren<ParticleSystem>(true);
            particleCount = particles.Length;
            foreach (ParticleSystem particle in particles)
                particle.Simulate(input.sample_time, true, true, true);

            Renderer[] renderers = instance.GetComponentsInChildren<Renderer>(true);
            rendererCount = renderers.Length;
            bool hasWorldBounds = false;
            foreach (Renderer renderer in renderers)
            {
                foreach (Material material in renderer.sharedMaterials)
                {
                    if (material == null)
                    {
                        nullMaterialSlotCount++;
                        materialFacts.Add(renderer.name + " | <null> | <null> | supported=false");
                        continue;
                    }
                    string shaderName = material.shader != null ? material.shader.name : "<null>";
                    bool supported = material.shader != null && material.shader.isSupported;
                    if (!supported)
                        unsupportedShaderCount++;
                    materialFacts.Add(renderer.name + " | " + material.name + " | " + shaderName + " | supported=" + supported);
                }
                if (!renderer.enabled || !renderer.gameObject.activeInHierarchy)
                    continue;
                if (!hasWorldBounds)
                {
                    worldBounds = renderer.bounds;
                    hasWorldBounds = true;
                }
                else
                {
                    worldBounds.Encapsulate(renderer.bounds);
                }
            }
            materialFacts = materialFacts.Distinct(StringComparer.Ordinal).OrderBy(value => value, StringComparer.Ordinal).ToList();
            if (!hasWorldBounds)
                throw new InvalidOperationException("No enabled renderer produced bounds.");
            if (unsupportedShaderCount > 0)
                throw new InvalidOperationException("At least one non-null material shader is unsupported by the active graphics device.");

            GameObject cameraObject = new GameObject("ArknightsEffectRendererCamera");
            Camera camera = cameraObject.AddComponent<Camera>();
            camera.orthographic = true;
            camera.clearFlags = CameraClearFlags.SolidColor;
            camera.backgroundColor = Color.black;
            camera.nearClipPlane = 0.01f;
            camera.farClipPlane = 200f;
            camera.transform.position = new Vector3(worldBounds.center.x, worldBounds.center.y, worldBounds.min.z - 50f);
            camera.transform.rotation = Quaternion.identity;
            camera.orthographicSize = Math.Max(0.5f, Math.Max(worldBounds.extents.y, worldBounds.extents.x) * 1.25f);

            const int sourceSize = 1024;
            var sourceTarget = new RenderTexture(sourceSize, sourceSize, 24, RenderTextureFormat.ARGB32);
            var sourceTexture = new Texture2D(sourceSize, sourceSize, TextureFormat.RGBA32, false);
            camera.targetTexture = sourceTarget;
            camera.Render();
            RenderTexture previous = RenderTexture.active;
            RenderTexture.active = sourceTarget;
            sourceTexture.ReadPixels(new Rect(0, 0, sourceSize, sourceSize), 0, 0, false);
            sourceTexture.Apply(false, false);
            string fullImage = Path.Combine(Path.GetDirectoryName(input.output_image) ?? string.Empty, "effect_frame_full.png");
            File.WriteAllBytes(fullImage, sourceTexture.EncodeToPNG());

            Texture2D finalTexture = CropScaleAndRecoverAlpha(sourceTexture, out visibleBounds, out visiblePixelCount);
            File.WriteAllBytes(input.output_image, finalTexture.EncodeToPNG());

            RenderTexture.active = previous;
            camera.targetTexture = null;
            UnityEngine.Object.DestroyImmediate(finalTexture);
            UnityEngine.Object.DestroyImmediate(sourceTexture);
            UnityEngine.Object.DestroyImmediate(sourceTarget);
            UnityEngine.Object.DestroyImmediate(cameraObject);
            UnityEngine.Object.DestroyImmediate(instance);
            status = "passed_single_frame";
            exitCode = 0;
        }
        catch (Exception exception)
        {
            error = exception.ToString();
            Debug.LogException(exception);
        }
        finally
        {
            if (input != null && !string.IsNullOrWhiteSpace(input.output_report))
            {
                var report = new StringBuilder();
                report.AppendLine("{");
                report.AppendLine("  \"status\": \"" + Escape(status) + "\",");
                report.AppendLine("  \"unity_version\": \"" + Escape(Application.unityVersion) + "\",");
                report.AppendLine("  \"active_build_target\": \"" + Escape(EditorUserBuildSettings.activeBuildTarget.ToString()) + "\",");
                report.AppendLine("  \"graphics_device\": \"" + Escape(SystemInfo.graphicsDeviceType.ToString()) + "\",");
                report.AppendLine("  \"asset_path\": \"" + Escape(input.asset_path) + "\",");
                report.AppendLine("  \"sample_time_seconds\": " + input.sample_time.ToString(CultureInfo.InvariantCulture) + ",");
                report.AppendLine("  \"asset_count\": " + assetCount + ",");
                report.AppendLine("  \"loaded_bundle_count\": " + loadedPaths.Count + ",");
                report.AppendLine("  \"failed_bundle_count\": " + failedPaths.Count + ",");
                report.AppendLine("  \"renderer_count\": " + rendererCount + ",");
                report.AppendLine("  \"particle_system_count\": " + particleCount + ",");
                report.AppendLine("  \"unsupported_non_null_shader_count\": " + unsupportedShaderCount + ",");
                report.AppendLine("  \"null_material_slot_count\": " + nullMaterialSlotCount + ",");
                report.AppendLine("  \"visible_pixel_count\": " + visiblePixelCount + ",");
                report.AppendLine("  \"visible_pixel_bounds\": [" + visibleBounds.x + "," + visibleBounds.y + "," + visibleBounds.width + "," + visibleBounds.height + "],");
                report.AppendLine("  \"world_bounds_center\": " + VectorJson(worldBounds.center) + ",");
                report.AppendLine("  \"world_bounds_size\": " + VectorJson(worldBounds.size) + ",");
                report.AppendLine("  \"material_facts\": " + StringArrayJson(materialFacts) + ",");
                report.AppendLine("  \"loaded_bundles\": " + StringArrayJson(loadedPaths) + ",");
                report.AppendLine("  \"failed_bundles\": " + StringArrayJson(failedPaths) + ",");
                report.AppendLine("  \"output_image\": \"" + Escape(input.output_image) + "\",");
                report.AppendLine("  \"error\": " + (error == null ? "null" : "\"" + Escape(error) + "\"") );
                report.AppendLine("}");
                File.WriteAllText(input.output_report, report.ToString(), new UTF8Encoding(false));
            }
            for (int index = loaded.Count - 1; index >= 0; index--)
                loaded[index].Unload(true);
            EditorApplication.Exit(exitCode);
        }
    }

    private static Texture2D CropScaleAndRecoverAlpha(Texture2D source, out RectInt bounds, out int visibleCount)
    {
        Color32[] pixels = source.GetPixels32();
        int width = source.width;
        int height = source.height;
        int minX = width;
        int minY = height;
        int maxX = -1;
        int maxY = -1;
        visibleCount = 0;
        for (int y = 0; y < height; y++)
        {
            for (int x = 0; x < width; x++)
            {
                Color32 pixel = pixels[y * width + x];
                byte alpha = Math.Max(pixel.r, Math.Max(pixel.g, pixel.b));
                if (alpha <= 2)
                    continue;
                visibleCount++;
                minX = Math.Min(minX, x);
                minY = Math.Min(minY, y);
                maxX = Math.Max(maxX, x);
                maxY = Math.Max(maxY, y);
            }
        }
        if (visibleCount == 0)
            throw new InvalidOperationException("The rendered sample contains no visible pixels.");

        int contentWidth = maxX - minX + 1;
        int contentHeight = maxY - minY + 1;
        int padding = Math.Max(4, (int)Math.Ceiling(Math.Max(contentWidth, contentHeight) * 0.12f));
        minX = Math.Max(0, minX - padding);
        minY = Math.Max(0, minY - padding);
        maxX = Math.Min(width - 1, maxX + padding);
        maxY = Math.Min(height - 1, maxY + padding);
        bounds = new RectInt(minX, minY, maxX - minX + 1, maxY - minY + 1);

        Color[] cropPixels = source.GetPixels(bounds.x, bounds.y, bounds.width, bounds.height);
        for (int index = 0; index < cropPixels.Length; index++)
        {
            Color pixel = cropPixels[index];
            float alpha = Math.Max(pixel.r, Math.Max(pixel.g, pixel.b));
            cropPixels[index] = alpha <= (2f / 255f) ? Color.clear : new Color(pixel.r, pixel.g, pixel.b, alpha);
        }
        var crop = new Texture2D(bounds.width, bounds.height, TextureFormat.RGBA32, false);
        crop.SetPixels(cropPixels);
        crop.Apply(false, false);

        const int canvasSize = 512;
        const int contentLimit = 460;
        float scale = Math.Min((float)contentLimit / bounds.width, (float)contentLimit / bounds.height);
        int scaledWidth = Math.Max(1, Mathf.RoundToInt(bounds.width * scale));
        int scaledHeight = Math.Max(1, Mathf.RoundToInt(bounds.height * scale));
        var scaledTarget = new RenderTexture(scaledWidth, scaledHeight, 0, RenderTextureFormat.ARGB32);
        Graphics.Blit(crop, scaledTarget);
        RenderTexture previous = RenderTexture.active;
        RenderTexture.active = scaledTarget;
        var scaled = new Texture2D(scaledWidth, scaledHeight, TextureFormat.RGBA32, false);
        scaled.ReadPixels(new Rect(0, 0, scaledWidth, scaledHeight), 0, 0, false);
        scaled.Apply(false, false);
        RenderTexture.active = previous;

        var final = new Texture2D(canvasSize, canvasSize, TextureFormat.RGBA32, false);
        final.SetPixels32(Enumerable.Repeat(new Color32(0, 0, 0, 0), canvasSize * canvasSize).ToArray());
        int offsetX = (canvasSize - scaledWidth) / 2;
        int offsetY = (canvasSize - scaledHeight) / 2;
        final.SetPixels(offsetX, offsetY, scaledWidth, scaledHeight, scaled.GetPixels());
        final.Apply(false, false);

        UnityEngine.Object.DestroyImmediate(crop);
        UnityEngine.Object.DestroyImmediate(scaled);
        UnityEngine.Object.DestroyImmediate(scaledTarget);
        return final;
    }

    private static string GetArgument(string name)
    {
        string[] args = Environment.GetCommandLineArgs();
        for (int index = 0; index + 1 < args.Length; index++)
            if (string.Equals(args[index], name, StringComparison.OrdinalIgnoreCase))
                return args[index + 1];
        return null;
    }

    private static string VectorJson(Vector3 value) => string.Format(CultureInfo.InvariantCulture, "[{0},{1},{2}]", value.x, value.y, value.z);
    private static string StringArrayJson(IEnumerable<string> values) => "[" + string.Join(",", values.Select(value => "\"" + Escape(value) + "\"")) + "]";
    private static string Escape(string value) => (value ?? string.Empty).Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\r", "\\r").Replace("\n", "\\n");
}
