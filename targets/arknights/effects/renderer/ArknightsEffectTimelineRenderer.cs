using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using UnityEditor;
using UnityEngine;
using UnityEngine.Rendering;

public static class ArknightsEffectTimelineRenderer
{
    [Serializable]
    private sealed class RenderInput
    {
        public string main_bundle;
        public string[] dependency_bundles;
        public string asset_path;
        public string output_report;
        public string mode;
        public float duration_seconds;
        public int fps;
        public int max_frames;
        public int minimum_frames;
        public int empty_tail_frames;
        public float prewarm_ratio;
        public string frames_directory;
        public bool drive_shader_time;
        public string[] disabled_renderer_name_contains;
        // Optional externally evidenced projectile motion. This is deliberately
        // separate from prefab animation: a prefab-local timeline cannot invent
        // the projectile's travel path on its own.
        public bool external_projectile_motion;
        public Vector3 motion_start;
        public Vector3 motion_end;
        public string motion_source;
    }

    private sealed class FrameFact
    {
        public int Index;
        public float Time;
        public int VisiblePixels;
        public string Path;
        public string Sha256;
    }

    public static void Run()
    {
        int exitCode = 1;
        RenderInput input = null;
        var loaded = new List<AssetBundle>();
        var loadedPaths = new List<string>();
        var failedPaths = new List<string>();
        var materialFacts = new List<string>();
        var componentFacts = new List<string>();
        string rendererStructuredFacts = "[]";
        var frameFacts = new List<FrameFact>();
        var boundsProbeTimes = new List<float>();
        var disabledRendererFacts = new List<string>();
        string status = "failed";
        string error = null;
        float detectedDuration = 0f;
        float renderDuration = 0f;
        int unsupportedShaderCount = 0;
        int nullMaterialSlotCount = 0;
        int rendererCount = 0;
        int particleCount = 0;
        int animatorCount = 0;
        int legacyAnimationCount = 0;
        int animationClipCount = 0;
        int trailRendererCount = 0;
        int maxTrailPositionCount = 0;
        float maxTrailPathLength = 0f;
        float appliedMotionDistance = 0f;
        bool externalMotionApplied = false;
        bool hasLoopingParticle = false;
        Bounds renderBounds = new Bounds(Vector3.zero, Vector3.zero);

        try
        {
            string inputPath = GetArgument("-stageInput");
            if (string.IsNullOrWhiteSpace(inputPath) || !File.Exists(inputPath))
                throw new FileNotFoundException("Missing -stageInput JSON.", inputPath);
            input = JsonUtility.FromJson<RenderInput>(File.ReadAllText(inputPath, Encoding.UTF8));
            ApplyCommandLineOverrides(input);
            ValidateInput(input);
            Directory.CreateDirectory(input.frames_directory);
            foreach (string existing in Directory.GetFiles(input.frames_directory, "f*.png"))
                File.Delete(existing);

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

            string canonicalAsset = main.GetAllAssetNames().FirstOrDefault(item => string.Equals(item, input.asset_path, StringComparison.OrdinalIgnoreCase));
            if (canonicalAsset == null)
                throw new InvalidOperationException("Requested effect asset is absent from the main Bundle.");
            GameObject prefab = main.LoadAsset<GameObject>(canonicalAsset);
            if (prefab == null)
                throw new InvalidOperationException("Requested asset did not load as a GameObject prefab.");

            GameObject durationProbe = UnityEngine.Object.Instantiate(prefab);
            durationProbe.name = "ArknightsEffectDurationProbe";
            durationProbe.SetActive(true);
            rendererCount = durationProbe.GetComponentsInChildren<Renderer>(true).Length;
            particleCount = durationProbe.GetComponentsInChildren<ParticleSystem>(true).Length;
            animatorCount = durationProbe.GetComponentsInChildren<Animator>(true).Length;
            legacyAnimationCount = durationProbe.GetComponentsInChildren<Animation>(true).Length;
            detectedDuration = DetectDuration(durationProbe, out hasLoopingParticle, out animationClipCount);
            float minimumDuration = (float)input.minimum_frames / input.fps;
            float maximumDuration = (float)input.max_frames / input.fps;
            renderDuration = input.duration_seconds > 0f ? input.duration_seconds : detectedDuration;
            if (hasLoopingParticle && input.duration_seconds <= 0f)
                renderDuration = Math.Max(renderDuration, 3f);
            renderDuration = Mathf.Clamp(Math.Max(renderDuration, minimumDuration), minimumDuration, maximumDuration);
            UnityEngine.Object.DestroyImmediate(durationProbe);

            GameObject boundsProbe = UnityEngine.Object.Instantiate(prefab);
            boundsProbe.name = "ArknightsEffectBoundsProbe";
            boundsProbe.SetActive(true);
            ConfigureDeterministicPlayback(boundsProbe, 0x414B1000u);
            renderBounds = ProbeBounds(boundsProbe, renderDuration, input.fps, input.prewarm_ratio, boundsProbeTimes);
            CaptureComponentFacts(boundsProbe, componentFacts);
            UnityEngine.Object.DestroyImmediate(boundsProbe);
            if (renderBounds.size.sqrMagnitude <= 0.000001f)
                throw new InvalidOperationException("Particle prewarm produced no usable renderer bounds.");

            GameObject instance = UnityEngine.Object.Instantiate(prefab);
            instance.name = "ArknightsEffectTimelineInstance";
            instance.SetActive(true);
            Vector3 motionCenter = input.external_projectile_motion
                ? (input.motion_start + input.motion_end) * 0.5f
                : Vector3.zero;
            Vector3 centeredOrigin = instance.transform.position - renderBounds.center - motionCenter;
            instance.transform.position = centeredOrigin + (input.external_projectile_motion ? input.motion_start : Vector3.zero);
            ConfigureDeterministicPlayback(instance, 0x414B2000u);

            Renderer[] renderers = instance.GetComponentsInChildren<Renderer>(true);
            rendererStructuredFacts = RendererFactsJson(renderers, instance.transform);
            DisableRequestedRenderers(renderers, input.disabled_renderer_name_contains, disabledRendererFacts);
            ParticleSystem[] particles = instance.GetComponentsInChildren<ParticleSystem>(true);
            TrailRenderer[] trails = instance.GetComponentsInChildren<TrailRenderer>(true);
            trailRendererCount = trails.Length;
            rendererCount = renderers.Length;
            particleCount = particles.Length;
            InspectMaterials(renderers, materialFacts, ref unsupportedShaderCount, ref nullMaterialSlotCount);
            if (unsupportedShaderCount > 0)
                throw new InvalidOperationException("At least one non-null material shader is unsupported by the active graphics device.");

            GameObject cameraObject = new GameObject("ArknightsEffectTimelineCamera");
            Camera camera = cameraObject.AddComponent<Camera>();
            camera.orthographic = true;
            camera.clearFlags = CameraClearFlags.SolidColor;
            camera.backgroundColor = Color.black;
            camera.nearClipPlane = 0.3f;
            camera.farClipPlane = 1000f;
            camera.transform.position = new Vector3(0f, 0f, -50f);
            camera.transform.rotation = Quaternion.identity;
            Vector3 motionHalfRange = input.external_projectile_motion
                ? new Vector3(
                    Mathf.Abs(input.motion_end.x - input.motion_start.x) * 0.5f,
                    Mathf.Abs(input.motion_end.y - input.motion_start.y) * 0.5f,
                    Mathf.Abs(input.motion_end.z - input.motion_start.z) * 0.5f)
                : Vector3.zero;
            Vector3 cameraHalfExtents = renderBounds.extents + motionHalfRange;
            camera.orthographicSize = Math.Max(0.5f, Math.Max(cameraHalfExtents.y, cameraHalfExtents.x) * 1.15f);
            CommandBuffer shaderTimeCommands = null;
            if (input.drive_shader_time)
            {
                shaderTimeCommands = new CommandBuffer { name = "Arknights deterministic shader time" };
                camera.AddCommandBuffer(CameraEvent.BeforeForwardAlpha, shaderTimeCommands);
            }

            const int frameSize = 512;
            var target = new RenderTexture(frameSize, frameSize, 24, RenderTextureFormat.ARGB32);
            var texture = new Texture2D(frameSize, frameSize, TextureFormat.RGBA32, false);
            camera.targetTexture = target;
            RenderTexture previous = RenderTexture.active;
            float delta = 1f / input.fps;
            int plannedFrames = Math.Min(input.max_frames, Mathf.CeilToInt(renderDuration * input.fps) + 1);
            int consecutiveEmpty = 0;
            bool observedVisible = false;
            Vector3 previousMotionPosition = instance.transform.position;
            if (input.external_projectile_motion)
            {
                ResetAndSeedTrails(trails, instance.transform.position);
                externalMotionApplied = true;
            }
            for (int index = 0; index < plannedFrames; index++)
            {
                if (index > 0)
                    AdvancePlayback(instance, delta);
                if (input.external_projectile_motion)
                {
                    Vector3 nextMotionPosition = centeredOrigin + EvaluateMotion(input, index * delta, renderDuration);
                    appliedMotionDistance += Vector3.Distance(previousMotionPosition, nextMotionPosition);
                    instance.transform.position = nextMotionPosition;
                    AppendTrailPositions(trails, nextMotionPosition);
                    previousMotionPosition = nextMotionPosition;
                }
                if (input.drive_shader_time)
                    SetDeterministicShaderTime(index * delta, delta, shaderTimeCommands);
                camera.Render();
                RenderTexture.active = target;
                texture.ReadPixels(new Rect(0, 0, frameSize, frameSize), 0, 0, false);
                texture.Apply(false, false);
                int visiblePixels = RecoverAlpha(texture, 8);
                string framePath = Path.Combine(input.frames_directory, "f" + index.ToString("D4") + ".png");
                File.WriteAllBytes(framePath, texture.EncodeToPNG());
                frameFacts.Add(new FrameFact
                {
                    Index = index,
                    Time = index * delta,
                    VisiblePixels = visiblePixels,
                    Path = framePath,
                    Sha256 = FileSha256(framePath),
                });
                if (visiblePixels > 0)
                {
                    observedVisible = true;
                    consecutiveEmpty = 0;
                }
                else
                {
                    consecutiveEmpty++;
                }
                UpdateTrailFacts(trails, ref maxTrailPositionCount, ref maxTrailPathLength);
                if (observedVisible && frameFacts.Count >= input.minimum_frames && consecutiveEmpty >= input.empty_tail_frames)
                    break;
            }
            RenderTexture.active = previous;
            camera.targetTexture = null;
            if (shaderTimeCommands != null)
            {
                camera.RemoveCommandBuffer(CameraEvent.BeforeForwardAlpha, shaderTimeCommands);
                shaderTimeCommands.Release();
            }
            UnityEngine.Object.DestroyImmediate(texture);
            UnityEngine.Object.DestroyImmediate(target);
            UnityEngine.Object.DestroyImmediate(cameraObject);
            UnityEngine.Object.DestroyImmediate(instance);
            if (!observedVisible)
                throw new InvalidOperationException("The complete requested timeline contained no visible pixels.");

            if (trailRendererCount > 0 && !input.external_projectile_motion)
            {
                status = "partial_static_trail_no_motion_fixture";
                exitCode = 2;
            }
            else if (trailRendererCount > 0 && input.external_projectile_motion && maxTrailPositionCount < 2)
            {
                status = "failed_motion_fixture_did_not_create_trail";
                exitCode = 2;
            }
            else
            {
                status = "passed_timeline_frames";
                exitCode = 0;
            }
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
                report.AppendLine("  \"graphics_device\": \"" + Escape(SystemInfo.graphicsDeviceType.ToString()) + "\",");
                report.AppendLine("  \"asset_path\": \"" + Escape(input.asset_path) + "\",");
                report.AppendLine("  \"fps\": " + input.fps + ",");
                report.AppendLine("  \"requested_duration_seconds\": " + input.duration_seconds.ToString(CultureInfo.InvariantCulture) + ",");
                report.AppendLine("  \"detected_duration_seconds\": " + detectedDuration.ToString(CultureInfo.InvariantCulture) + ",");
                report.AppendLine("  \"render_duration_seconds\": " + renderDuration.ToString(CultureInfo.InvariantCulture) + ",");
                report.AppendLine("  \"frame_count\": " + frameFacts.Count + ",");
                report.AppendLine("  \"loaded_bundle_count\": " + loadedPaths.Count + ",");
                report.AppendLine("  \"failed_bundle_count\": " + failedPaths.Count + ",");
                report.AppendLine("  \"renderer_count\": " + rendererCount + ",");
                report.AppendLine("  \"particle_system_count\": " + particleCount + ",");
                report.AppendLine("  \"animator_count\": " + animatorCount + ",");
                report.AppendLine("  \"legacy_animation_count\": " + legacyAnimationCount + ",");
                report.AppendLine("  \"animation_clip_count\": " + animationClipCount + ",");
                report.AppendLine("  \"trail_renderer_count\": " + trailRendererCount + ",");
                report.AppendLine("  \"external_projectile_motion\": " + input.external_projectile_motion.ToString().ToLowerInvariant() + ",");
                report.AppendLine("  \"external_motion_applied\": " + externalMotionApplied.ToString().ToLowerInvariant() + ",");
                report.AppendLine("  \"motion_source\": " + (string.IsNullOrWhiteSpace(input.motion_source) ? "null" : "\"" + Escape(input.motion_source) + "\"") + ",");
                report.AppendLine("  \"motion_start\": " + VectorJson(input.motion_start) + ",");
                report.AppendLine("  \"motion_end\": " + VectorJson(input.motion_end) + ",");
                report.AppendLine("  \"applied_motion_distance\": " + appliedMotionDistance.ToString(CultureInfo.InvariantCulture) + ",");
                report.AppendLine("  \"max_trail_position_count\": " + maxTrailPositionCount + ",");
                report.AppendLine("  \"max_trail_path_length\": " + maxTrailPathLength.ToString(CultureInfo.InvariantCulture) + ",");
                report.AppendLine("  \"has_looping_particle\": " + hasLoopingParticle.ToString().ToLowerInvariant() + ",");
                report.AppendLine("  \"unsupported_non_null_shader_count\": " + unsupportedShaderCount + ",");
                report.AppendLine("  \"null_material_slot_count\": " + nullMaterialSlotCount + ",");
                report.AppendLine("  \"drive_shader_time\": " + input.drive_shader_time.ToString().ToLowerInvariant() + ",");
                report.AppendLine("  \"disabled_renderers\": " + StringArrayJson(disabledRendererFacts) + ",");
                report.AppendLine("  \"bounds_probe_times\": " + FloatArrayJson(boundsProbeTimes) + ",");
                report.AppendLine("  \"render_bounds_center\": " + VectorJson(renderBounds.center) + ",");
                report.AppendLine("  \"render_bounds_size\": " + VectorJson(renderBounds.size) + ",");
                report.AppendLine("  \"material_facts\": " + StringArrayJson(materialFacts) + ",");
                report.AppendLine("  \"component_facts\": " + StringArrayJson(componentFacts) + ",");
                report.AppendLine("  \"renderer_structured_facts\": " + rendererStructuredFacts + ",");
                report.AppendLine("  \"loaded_bundles\": " + StringArrayJson(loadedPaths) + ",");
                report.AppendLine("  \"failed_bundles\": " + StringArrayJson(failedPaths) + ",");
                report.AppendLine("  \"frames_directory\": \"" + Escape(input.frames_directory) + "\",");
                report.AppendLine("  \"frames\": " + FramesJson(frameFacts) + ",");
                report.AppendLine("  \"error\": " + (error == null ? "null" : "\"" + Escape(error) + "\"") );
                report.AppendLine("}");
                File.WriteAllText(input.output_report, report.ToString(), new UTF8Encoding(false));
            }
            for (int index = loaded.Count - 1; index >= 0; index--)
                loaded[index].Unload(true);
            EditorApplication.Exit(exitCode);
        }
    }

    private static void ValidateInput(RenderInput input)
    {
        if (input == null || input.mode != "timeline")
            throw new InvalidDataException("The timeline renderer requires mode=timeline.");
        if (string.IsNullOrWhiteSpace(input.main_bundle) || string.IsNullOrWhiteSpace(input.asset_path) || string.IsNullOrWhiteSpace(input.frames_directory))
            throw new InvalidDataException("Invalid timeline stage input.");
        if (input.fps < 1 || input.fps > 120 || input.max_frames < 1 || input.minimum_frames < 1 || input.empty_tail_frames < 1)
            throw new InvalidDataException("Invalid timeline limits.");
        if (input.external_projectile_motion)
        {
            if (string.IsNullOrWhiteSpace(input.motion_source))
                throw new InvalidDataException("External projectile motion requires motion_source evidence.");
            if ((input.motion_end - input.motion_start).sqrMagnitude <= 0.000001f)
                throw new InvalidDataException("External projectile motion requires distinct start and end positions.");
        }
    }

    private static void ApplyCommandLineOverrides(RenderInput input)
    {
        string framesDirectory = GetArgument("-framesDirectory");
        string outputReport = GetArgument("-outputReport");
        string driveShaderTime = GetArgument("-driveShaderTime");
        string disabledRenderers = GetArgument("-disableRendererContains");
        string maxFrames = GetArgument("-maxFrames");
        string minimumFrames = GetArgument("-minimumFrames");
        if (!string.IsNullOrWhiteSpace(framesDirectory)) input.frames_directory = framesDirectory;
        if (!string.IsNullOrWhiteSpace(outputReport)) input.output_report = outputReport;
        if (!string.IsNullOrWhiteSpace(driveShaderTime))
            input.drive_shader_time = string.Equals(driveShaderTime, "true", StringComparison.OrdinalIgnoreCase) || driveShaderTime == "1";
        if (!string.IsNullOrWhiteSpace(disabledRenderers))
            input.disabled_renderer_name_contains = disabledRenderers.Split(new[] { ';' }, StringSplitOptions.RemoveEmptyEntries);
        if (int.TryParse(maxFrames, out int parsedMaxFrames)) input.max_frames = parsedMaxFrames;
        if (int.TryParse(minimumFrames, out int parsedMinimumFrames)) input.minimum_frames = parsedMinimumFrames;
    }

    private static float DetectDuration(GameObject instance, out bool hasLoopingParticle, out int animationClipCount)
    {
        float duration = 0f;
        hasLoopingParticle = false;
        var clips = new HashSet<AnimationClip>();
        foreach (Animator animator in instance.GetComponentsInChildren<Animator>(true))
            if (animator.runtimeAnimatorController != null)
                foreach (AnimationClip clip in animator.runtimeAnimatorController.animationClips)
                    if (clip != null) clips.Add(clip);
        foreach (Animation animation in instance.GetComponentsInChildren<Animation>(true))
            foreach (AnimationState state in animation)
                if (state.clip != null) clips.Add(state.clip);
        foreach (AnimationClip clip in clips)
            duration = Math.Max(duration, clip.length);
        animationClipCount = clips.Count;
        foreach (ParticleSystem particle in instance.GetComponentsInChildren<ParticleSystem>(true))
        {
            ParticleSystem.MainModule main = particle.main;
            hasLoopingParticle |= main.loop;
            duration = Math.Max(duration, main.startDelay.constantMax + main.duration + main.startLifetime.constantMax);
        }
        return Math.Max(duration, 1f);
    }

    private static void ConfigureDeterministicPlayback(GameObject instance, uint seedBase)
    {
        foreach (Animator animator in instance.GetComponentsInChildren<Animator>(true))
        {
            animator.enabled = true;
            animator.Rebind();
            AnimationClip firstClip = animator.runtimeAnimatorController != null
                ? animator.runtimeAnimatorController.animationClips.FirstOrDefault(clip => clip != null)
                : null;
            if (firstClip != null)
                animator.Play(firstClip.name, 0, 0f);
            animator.Update(0f);
        }
        foreach (Animation animation in instance.GetComponentsInChildren<Animation>(true))
        {
            animation.enabled = true;
            animation.Stop();
            foreach (AnimationState state in animation)
            {
                state.enabled = true;
                state.weight = 1f;
                state.time = 0f;
            }
            animation.Sample();
        }
        int index = 0;
        foreach (ParticleSystem particle in instance.GetComponentsInChildren<ParticleSystem>(true))
        {
            particle.Stop(true, ParticleSystemStopBehavior.StopEmittingAndClear);
            particle.useAutoRandomSeed = false;
            particle.randomSeed = seedBase + (uint)index++;
            particle.Play(true);
        }
    }

    private static void AdvancePlayback(GameObject instance, float delta)
    {
        foreach (Animator animator in instance.GetComponentsInChildren<Animator>(true))
            animator.Update(delta);
        foreach (Animation animation in instance.GetComponentsInChildren<Animation>(true))
        {
            foreach (AnimationState state in animation)
                state.time += delta;
            animation.Sample();
        }
        foreach (ParticleSystem particle in instance.GetComponentsInChildren<ParticleSystem>(true))
            particle.Simulate(delta, true, false, true);
    }

    private static Vector3 EvaluateMotion(RenderInput input, float time, float duration)
    {
        float t = duration <= 0.000001f ? 1f : Mathf.Clamp01(time / duration);
        return Vector3.Lerp(input.motion_start, input.motion_end, t);
    }

    private static void ResetAndSeedTrails(TrailRenderer[] trails, Vector3 position)
    {
        foreach (TrailRenderer trail in trails)
        {
            trail.Clear();
            trail.AddPosition(position);
        }
    }

    private static void AppendTrailPositions(TrailRenderer[] trails, Vector3 position)
    {
        foreach (TrailRenderer trail in trails)
            trail.AddPosition(position);
    }

    private static void UpdateTrailFacts(TrailRenderer[] trails, ref int maxPositionCount, ref float maxPathLength)
    {
        foreach (TrailRenderer trail in trails)
        {
            int count = trail.positionCount;
            maxPositionCount = Math.Max(maxPositionCount, count);
            if (count < 2)
                continue;
            Vector3[] positions = new Vector3[count];
            int actual = trail.GetPositions(positions);
            float length = 0f;
            for (int index = 1; index < actual; index++)
                length += Vector3.Distance(positions[index - 1], positions[index]);
            maxPathLength = Math.Max(maxPathLength, length);
        }
    }

    private static void SetDeterministicShaderTime(float time, float delta, CommandBuffer commands)
    {
        // Camera.Render in a tight editor batch loop does not advance Unity's
        // wall-clock shader globals at the simulated particle/animation rate.
        // Recreate Unity's documented time vectors from the explicit timeline.
        float safeDelta = Math.Max(delta, 0.000001f);
        Shader.SetGlobalVector("_Time", new Vector4(time / 20f, time, time * 2f, time * 3f));
        Shader.SetGlobalVector("_SinTime", new Vector4(
            Mathf.Sin(time / 8f), Mathf.Sin(time / 4f), Mathf.Sin(time / 2f), Mathf.Sin(time)));
        Shader.SetGlobalVector("_CosTime", new Vector4(
            Mathf.Cos(time / 8f), Mathf.Cos(time / 4f), Mathf.Cos(time / 2f), Mathf.Cos(time)));
        Shader.SetGlobalVector("unity_DeltaTime", new Vector4(safeDelta, 1f / safeDelta, safeDelta, 1f / safeDelta));
        Shader.SetGlobalVector("_TimeParameters", new Vector4(time, Mathf.Sin(time), Mathf.Cos(time), 0f));
        if (commands == null)
            return;
        commands.Clear();
        commands.SetGlobalVector("_Time", new Vector4(time / 20f, time, time * 2f, time * 3f));
        commands.SetGlobalVector("_SinTime", new Vector4(
            Mathf.Sin(time / 8f), Mathf.Sin(time / 4f), Mathf.Sin(time / 2f), Mathf.Sin(time)));
        commands.SetGlobalVector("_CosTime", new Vector4(
            Mathf.Cos(time / 8f), Mathf.Cos(time / 4f), Mathf.Cos(time / 2f), Mathf.Cos(time)));
        commands.SetGlobalVector("unity_DeltaTime", new Vector4(safeDelta, 1f / safeDelta, safeDelta, 1f / safeDelta));
        commands.SetGlobalVector("_TimeParameters", new Vector4(time, Mathf.Sin(time), Mathf.Cos(time), 0f));
    }

    private static void DisableRequestedRenderers(Renderer[] renderers, string[] patterns, List<string> facts)
    {
        string[] activePatterns = (patterns ?? Array.Empty<string>())
            .Where(value => !string.IsNullOrWhiteSpace(value))
            .ToArray();
        if (activePatterns.Length == 0)
            return;
        foreach (Renderer renderer in renderers)
        {
            string path = HierarchyPath(renderer.transform, renderer.transform.root);
            if (!activePatterns.Any(pattern => path.IndexOf(pattern, StringComparison.OrdinalIgnoreCase) >= 0))
                continue;
            renderer.enabled = false;
            facts.Add(path);
        }
        facts.Sort(StringComparer.Ordinal);
    }

    private static Bounds ProbeBounds(GameObject instance, float duration, int fps, float preferredRatio, List<float> sampleTimes)
    {
        // Short hit effects can emit and die before a single 20%/60% probe.
        // Scan a bounded set of deterministic times while retaining the workflow's
        // documented 20%, preferred (normally 60%), and 100% checkpoints.
        int scanCount = Math.Min(90, Math.Max(3, Mathf.CeilToInt(duration * fps)));
        var timeSet = new SortedSet<float>();
        for (int index = 1; index <= scanCount; index++)
            timeSet.Add(duration * index / scanCount);
        timeSet.Add(duration * 0.2f);
        timeSet.Add(duration * Mathf.Clamp01(preferredRatio));
        timeSet.Add(duration);
        float previous = 0f;
        bool hasBounds = false;
        Bounds bounds = new Bounds(Vector3.zero, Vector3.zero);
        foreach (float time in timeSet)
        {
            AdvancePlayback(instance, Math.Max(0f, time - previous));
            previous = time;
            sampleTimes.Add(time);
            foreach (Renderer renderer in instance.GetComponentsInChildren<Renderer>(true))
            {
                if (!renderer.enabled || !renderer.gameObject.activeInHierarchy || renderer.bounds.size.sqrMagnitude <= 0.000001f)
                    continue;
                if (!hasBounds) { bounds = renderer.bounds; hasBounds = true; }
                else bounds.Encapsulate(renderer.bounds);
            }
        }
        return hasBounds ? bounds : new Bounds(Vector3.zero, Vector3.zero);
    }

    private static void InspectMaterials(Renderer[] renderers, List<string> facts, ref int unsupportedCount, ref int nullCount)
    {
        foreach (Renderer renderer in renderers)
            foreach (Material material in renderer.sharedMaterials)
            {
                if (material == null)
                {
                    nullCount++;
                    facts.Add(renderer.name + " | <null> | <null> | supported=false");
                    continue;
                }
                string shader = material.shader != null ? material.shader.name : "<null>";
                bool supported = material.shader != null && material.shader.isSupported;
                if (!supported) unsupportedCount++;
                string textureBindings = string.Join(";", material.GetTexturePropertyNames().Select(property =>
                {
                    Texture texture = material.GetTexture(property);
                    return property + "=" + (texture != null ? texture.name : "<null>");
                }));
                facts.Add(renderer.name + " | " + material.name + " | " + shader + " | supported=" + supported + " | textures=" + textureBindings);
            }
        var unique = facts.Distinct(StringComparer.Ordinal).OrderBy(value => value, StringComparer.Ordinal).ToArray();
        facts.Clear();
        facts.AddRange(unique);
    }

    private static void CaptureComponentFacts(GameObject instance, List<string> facts)
    {
        foreach (Animator animator in instance.GetComponentsInChildren<Animator>(true))
        {
            string clips = animator.runtimeAnimatorController == null
                ? string.Empty
                : string.Join("|", animator.runtimeAnimatorController.animationClips.Where(clip => clip != null).Select(clip => clip.name));
            facts.Add("Animator " + HierarchyPath(animator.transform, instance.transform) + " activeSelf=" + animator.gameObject.activeSelf + " activeInHierarchy=" + animator.gameObject.activeInHierarchy + " enabled=" + animator.enabled + " clips=" + clips);
        }
        foreach (ParticleSystem particle in instance.GetComponentsInChildren<ParticleSystem>(true))
            facts.Add("Particle " + HierarchyPath(particle.transform, instance.transform) + " activeSelf=" + particle.gameObject.activeSelf + " activeInHierarchy=" + particle.gameObject.activeInHierarchy + " playing=" + particle.isPlaying + " alive=" + particle.IsAlive(true));
        foreach (Renderer renderer in instance.GetComponentsInChildren<Renderer>(true))
            facts.Add("Renderer " + HierarchyPath(renderer.transform, instance.transform) + " activeSelf=" + renderer.gameObject.activeSelf + " activeInHierarchy=" + renderer.gameObject.activeInHierarchy + " enabled=" + renderer.enabled + " bounds=" + renderer.bounds.size.ToString("R"));
        facts.Sort(StringComparer.Ordinal);
    }

    private static string RendererFactsJson(IEnumerable<Renderer> renderers, Transform root)
    {
        var rows = new List<string>();
        foreach (Renderer renderer in renderers.OrderBy(item => HierarchyPath(item.transform, root), StringComparer.Ordinal))
        {
            Material[] materials = renderer.sharedMaterials ?? Array.Empty<Material>();
            string materialJson = "[" + string.Join(",", materials.Select(material =>
            {
                if (material == null)
                    return "{\"name\":\"<null>\",\"shader\":\"<null>\",\"render_queue\":null}";
                string shader = material.shader != null ? material.shader.name : "<null>";
                return "{\"name\":\"" + Escape(material.name) + "\",\"shader\":\"" + Escape(shader) + "\",\"render_queue\":" + material.renderQueue.ToString(CultureInfo.InvariantCulture) + "}";
            })) + "]";
            Bounds bounds = renderer.bounds;
            rows.Add("{"
                + "\"path\":\"" + Escape(HierarchyPath(renderer.transform, root)) + "\","
                + "\"renderer_type\":\"" + Escape(renderer.GetType().FullName) + "\","
                + "\"game_object_active_self\":" + renderer.gameObject.activeSelf.ToString().ToLowerInvariant() + ","
                + "\"game_object_active_in_hierarchy\":" + renderer.gameObject.activeInHierarchy.ToString().ToLowerInvariant() + ","
                + "\"enabled\":" + renderer.enabled.ToString().ToLowerInvariant() + ","
                + "\"sorting_layer_id\":" + renderer.sortingLayerID.ToString(CultureInfo.InvariantCulture) + ","
                + "\"sorting_layer_name\":\"" + Escape(renderer.sortingLayerName) + "\","
                + "\"sorting_order\":" + renderer.sortingOrder.ToString(CultureInfo.InvariantCulture) + ","
                + "\"local_position\":" + VectorJson(renderer.transform.localPosition) + ","
                + "\"world_position\":" + VectorJson(renderer.transform.position) + ","
                + "\"local_scale\":" + VectorJson(renderer.transform.localScale) + ","
                + "\"lossy_scale\":" + VectorJson(renderer.transform.lossyScale) + ","
                + "\"bounds_center\":" + VectorJson(bounds.center) + ","
                + "\"bounds_size\":" + VectorJson(bounds.size) + ","
                + "\"materials\":" + materialJson
                + "}");
        }
        return "[" + string.Join(",", rows) + "]";
    }

    private static string HierarchyPath(Transform transform, Transform root)
    {
        var names = new List<string>();
        Transform current = transform;
        while (current != null)
        {
            names.Add(current.name);
            if (current == root) break;
            current = current.parent;
        }
        names.Reverse();
        return string.Join("/", names);
    }

    private static int RecoverAlpha(Texture2D texture, byte threshold)
    {
        Color32[] pixels = texture.GetPixels32();
        int visible = 0;
        for (int index = 0; index < pixels.Length; index++)
        {
            Color32 pixel = pixels[index];
            byte alpha = Math.Max(pixel.r, Math.Max(pixel.g, pixel.b));
            if (alpha <= threshold)
                pixels[index] = new Color32(0, 0, 0, 0);
            else
            {
                pixel.a = alpha;
                pixels[index] = pixel;
                visible++;
            }
        }
        texture.SetPixels32(pixels);
        texture.Apply(false, false);
        return visible;
    }

    private static string FileSha256(string path)
    {
        using (SHA256 algorithm = SHA256.Create())
        using (FileStream stream = File.OpenRead(path))
            return BitConverter.ToString(algorithm.ComputeHash(stream)).Replace("-", string.Empty);
    }

    private static string GetArgument(string name)
    {
        string[] args = Environment.GetCommandLineArgs();
        for (int index = 0; index + 1 < args.Length; index++)
            if (string.Equals(args[index], name, StringComparison.OrdinalIgnoreCase))
                return args[index + 1];
        return null;
    }

    private static string FramesJson(IEnumerable<FrameFact> frames) => "[" + string.Join(",", frames.Select(frame => "{\"index\":" + frame.Index + ",\"time_seconds\":" + frame.Time.ToString(CultureInfo.InvariantCulture) + ",\"visible_pixels\":" + frame.VisiblePixels + ",\"path\":\"" + Escape(frame.Path) + "\",\"sha256\":\"" + frame.Sha256 + "\"}")) + "]";
    private static string FloatArrayJson(IEnumerable<float> values) => "[" + string.Join(",", values.Select(value => value.ToString(CultureInfo.InvariantCulture))) + "]";
    private static string VectorJson(Vector3 value) => string.Format(CultureInfo.InvariantCulture, "[{0},{1},{2}]", value.x, value.y, value.z);
    private static string StringArrayJson(IEnumerable<string> values) => "[" + string.Join(",", values.Select(value => "\"" + Escape(value) + "\"")) + "]";
    private static string Escape(string value) => (value ?? string.Empty).Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\r", "\\r").Replace("\n", "\\n");
}
