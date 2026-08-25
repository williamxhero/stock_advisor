using System.Text.Json;

namespace AIDecisionCenter.App.Services;

public static class WindowStateService
{
    private static readonly JsonSerializerOptions JsonOptions = new() { WriteIndented = true };

    public static SavedWindowSize? Load(AppPaths paths)
    {
        if (!File.Exists(paths.WindowStatePath))
        {
            return null;
        }

        try
        {
            var state = JsonSerializer.Deserialize<SavedWindowSize>(File.ReadAllText(paths.WindowStatePath), JsonOptions);
            return state is { Width: >= 1, Height: >= 1 } &&
                   double.IsFinite(state.Width) && double.IsFinite(state.Height) &&
                   (state.Left is null || double.IsFinite(state.Left.Value)) &&
                   (state.Top is null || double.IsFinite(state.Top.Value))
                ? state
                : null;
        }
        catch (JsonException)
        {
            return null;
        }
        catch (IOException)
        {
            return null;
        }
    }

    public static void Save(AppPaths paths, double width, double height, double? left = null, double? top = null)
    {
        if (!double.IsFinite(width) || !double.IsFinite(height) || width < 1 || height < 1 ||
            (left is not null && !double.IsFinite(left.Value)) ||
            (top is not null && !double.IsFinite(top.Value)))
        {
            return;
        }

        paths.EnsureDirectories();
        var temporaryPath = paths.WindowStatePath + ".tmp";
        File.WriteAllText(
            temporaryPath,
            JsonSerializer.Serialize(new SavedWindowSize(width, height, left, top), JsonOptions),
            System.Text.Encoding.UTF8);
        File.Move(temporaryPath, paths.WindowStatePath, overwrite: true);
    }
}

public sealed record SavedWindowSize(double Width, double Height, double? Left = null, double? Top = null);
