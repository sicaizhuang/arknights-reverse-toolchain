using System;
using System.Collections.Generic;
using System.Collections.Specialized;
using System.Security.Cryptography;

namespace AnimeStudio;

/// <summary>
/// Common read-only envelope for Unity ParticleSystem assets.  Unity changes
/// this type tree frequently, so the parser deliberately keeps both the
/// decoded TypeTree and an exact object-byte span.  Unknown fields are never
/// assigned guessed meanings.
/// </summary>
public abstract class ParticleAsset : Object
{
    public string parser_status { get; protected set; } = "raw_only";
    public int class_id => (int)type;
    public long path_id => m_PathID;
    public uint serialized_byte_size => byteSize;
    public string raw_sha256 { get; protected set; }
    public string raw_hex { get; protected set; }
    public string type_tree_hash { get; protected set; }
    public object decoded_type_tree { get; protected set; }
    public List<RawFieldSpan> raw_spans { get; } = new();
    public List<ParticleObjectReference> object_references { get; } = new();

    protected ParticleAsset(ObjectReader reader) : base(reader)
    {
        var raw = GetRawData();
        raw_sha256 = Convert.ToHexString(SHA256.HashData(raw));
        raw_hex = Convert.ToHexString(raw);
        raw_spans.Add(new RawFieldSpan
        {
            field = "serialized_object",
            relative_offset = 0,
            length = raw.Length,
            hex = raw_hex,
            interpretation = "exact object bytes; field semantics are not assumed"
        });

        if (serializedType?.m_Type == null)
        {
            parser_status = "raw_only_missing_type_tree";
            return;
        }

        try
        {
            // Dumping through the retained Unity TypeTree is intentionally
            // generic.  It covers version-specific modules without hardcoded
            // offsets and retains raw bytes above for every field we cannot
            // explain.
            var spans = new List<TypeTreeReadSpan>();
            decoded_type_tree = TypeTreeHelper.ReadType(serializedType.m_Type, reader, spans);
            type_tree_hash = TypeTreeFingerprint(serializedType.m_Type);
            foreach (var span in spans)
            {
                if (!span.is_leaf)
                    continue;
                if (span.relative_offset < 0 || span.length < 0 || span.relative_offset + span.length > raw.Length)
                    continue;
                raw_spans.Add(new RawFieldSpan
                {
                    field = span.field_path,
                    field_type = span.field_type,
                    relative_offset = span.relative_offset,
                    length = span.length,
                    hex = Convert.ToHexString(raw.AsSpan((int)span.relative_offset, span.length)),
                    interpretation = "TypeTree-defined serialized field; no extra semantic assumptions",
                    aligned = span.aligned
                });
            }
            CollectObjectReferences(decoded_type_tree, string.Empty);
            parser_status = "parser_supported_type_tree_with_field_spans";
        }
        catch (Exception ex)
        {
            parser_status = "raw_only_type_tree_decode_failed";
            raw_spans.Add(new RawFieldSpan
            {
                field = "type_tree_decode_error",
                relative_offset = 0,
                length = raw.Length,
                hex = raw_hex,
                interpretation = ex.GetType().Name + ": " + ex.Message
            });
        }
    }

    private void CollectObjectReferences(object value, string path)
    {
        if (value is OrderedDictionary dictionary)
        {
            if (dictionary.Contains("m_FileID") && dictionary.Contains("m_PathID"))
            {
                object_references.Add(new ParticleObjectReference
                {
                    field_path = path,
                    file_id = Convert.ToInt64(dictionary["m_FileID"]),
                    path_id = Convert.ToInt64(dictionary["m_PathID"]),
                    external = ResolveExternal(Convert.ToInt64(dictionary["m_FileID"]))
                });
            }
            foreach (System.Collections.DictionaryEntry entry in dictionary)
            {
                var child = string.IsNullOrEmpty(path) ? entry.Key.ToString() : $"{path}.{entry.Key}";
                CollectObjectReferences(entry.Value, child);
            }
        }
        else if (value is System.Collections.IList list)
        {
            for (var i = 0; i < list.Count; i++)
                CollectObjectReferences(list[i], $"{path}[{i}]");
        }
    }

    private ParticleExternalReference ResolveExternal(long fileId)
    {
        if (fileId <= 0 || fileId > assetsFile.m_Externals.Count)
            return null;
        var external = assetsFile.m_Externals[(int)fileId - 1];
        return new ParticleExternalReference
        {
            guid = external.guid.ToString(),
            type = external.type,
            path_name = external.pathName,
            file_name = external.fileName
        };
    }

    private static string TypeTreeFingerprint(TypeTree typeTree)
    {
        var text = string.Join("\n", typeTree.m_Nodes.ConvertAll(n =>
            $"{n.m_Level}:{n.m_Type}:{n.m_Name}:{n.m_ByteSize}:{n.m_MetaFlag}:{n.m_Version}"));
        return Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(
            System.Text.Encoding.UTF8.GetBytes(text)));
    }
}

public sealed class ParticleSystemAsset : ParticleAsset
{
    public ParticleSystemAsset(ObjectReader reader) : base(reader) { }
}

public sealed class ParticleSystemRendererAsset : ParticleAsset
{
    public ParticleSystemRendererAsset(ObjectReader reader) : base(reader) { }
}

public sealed class RawFieldSpan
{
    public string field { get; set; }
    public string field_type { get; set; }
    public long relative_offset { get; set; }
    public int length { get; set; }
    public string hex { get; set; }
    public string interpretation { get; set; }
    public bool aligned { get; set; }
}

public sealed class ParticleObjectReference
{
    public string field_path { get; set; }
    public long file_id { get; set; }
    public long path_id { get; set; }
    public ParticleExternalReference external { get; set; }
}

public sealed class ParticleExternalReference
{
    public string guid { get; set; }
    public int type { get; set; }
    public string path_name { get; set; }
    public string file_name { get; set; }
}
