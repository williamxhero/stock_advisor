using System.Globalization;
using AIDecisionCenter.Core.Models;
using Microsoft.Data.Sqlite;

namespace AIDecisionCenter.App.Services;

public sealed class SqliteTaskMessageStore : ITaskMessageStore
{
    private readonly string _connectionString;

    public SqliteTaskMessageStore(AppPaths paths)
    {
        paths.EnsureDirectories();
        _connectionString = new SqliteConnectionStringBuilder
        {
            DataSource = paths.DatabasePath,
            Mode = SqliteOpenMode.ReadWriteCreate,
            Cache = SqliteCacheMode.Shared,
            Pooling = false
        }.ToString();
    }

    public async Task InitializeAsync(CancellationToken cancellationToken = default)
    {
        await using var connection = await OpenAsync(cancellationToken).ConfigureAwait(false);
        var command = connection.CreateCommand();
        command.CommandText = """
            CREATE TABLE IF NOT EXISTS task_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                external_id TEXT NOT NULL UNIQUE,
                project TEXT NOT NULL,
                slot TEXT NOT NULL,
                task_type TEXT NOT NULL,
                scheduled_date TEXT NOT NULL,
                received_at TEXT NOT NULL,
                subject TEXT NOT NULL,
                body_markdown TEXT NOT NULL,
                is_read INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS ix_task_messages_scheduled_date
                ON task_messages(scheduled_date, slot);
            DELETE FROM task_messages
            WHERE external_id LIKE 'demo-%';
            """;
        await command.ExecuteNonQueryAsync(cancellationToken).ConfigureAwait(false);
    }

    public async Task<bool> AddAsync(IncomingTaskMessage incoming, ParsedTaskSubject parsed, CancellationToken cancellationToken = default)
    {
        await using var connection = await OpenAsync(cancellationToken).ConfigureAwait(false);
        var command = connection.CreateCommand();
        command.CommandText = """
            INSERT OR IGNORE INTO task_messages
                (external_id, project, slot, task_type, scheduled_date, received_at, subject, body_markdown, is_read)
            VALUES
                ($external_id, $project, $slot, $task_type, $scheduled_date, $received_at, $subject, $body_markdown, 0);
            SELECT changes();
            """;
        command.Parameters.AddWithValue("$external_id", incoming.ExternalId);
        command.Parameters.AddWithValue("$project", parsed.Project);
        command.Parameters.AddWithValue("$slot", parsed.Slot.ToString("HH:mm", CultureInfo.InvariantCulture));
        command.Parameters.AddWithValue("$task_type", parsed.TaskType);
        command.Parameters.AddWithValue("$scheduled_date", parsed.ScheduledDate.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture));
        command.Parameters.AddWithValue("$received_at", incoming.ReceivedAt.ToUniversalTime().ToString("O", CultureInfo.InvariantCulture));
        command.Parameters.AddWithValue("$subject", incoming.Subject);
        command.Parameters.AddWithValue("$body_markdown", incoming.BodyMarkdown);
        var result = await command.ExecuteScalarAsync(cancellationToken).ConfigureAwait(false);
        return Convert.ToInt32(result, CultureInfo.InvariantCulture) == 1;
    }

    public async Task<IReadOnlyList<TaskMessage>> GetForDateAsync(DateOnly scheduledDate, CancellationToken cancellationToken = default)
    {
        var messages = new List<TaskMessage>();
        await using var connection = await OpenAsync(cancellationToken).ConfigureAwait(false);
        var command = connection.CreateCommand();
        command.CommandText = """
            SELECT id, external_id, project, slot, task_type, scheduled_date, received_at, subject, body_markdown, is_read
            FROM task_messages
            WHERE scheduled_date = $scheduled_date
            ORDER BY slot, received_at DESC;
            """;
        command.Parameters.AddWithValue("$scheduled_date", scheduledDate.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture));

        await using var reader = await command.ExecuteReaderAsync(cancellationToken).ConfigureAwait(false);
        while (await reader.ReadAsync(cancellationToken).ConfigureAwait(false))
        {
            messages.Add(new TaskMessage(
                reader.GetInt64(0),
                reader.GetString(1),
                reader.GetString(2),
                TimeOnly.ParseExact(reader.GetString(3), "HH:mm", CultureInfo.InvariantCulture),
                reader.GetString(4),
                DateOnly.ParseExact(reader.GetString(5), "yyyy-MM-dd", CultureInfo.InvariantCulture),
                DateTimeOffset.Parse(reader.GetString(6), CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind),
                reader.GetString(7),
                reader.GetString(8),
                reader.GetInt64(9) != 0));
        }

        return messages;
    }

    public async Task<IReadOnlyList<TaskMessage>> GetAllAsync(CancellationToken cancellationToken = default)
    {
        var messages = new List<TaskMessage>();
        await using var connection = await OpenAsync(cancellationToken).ConfigureAwait(false);
        var command = connection.CreateCommand();
        command.CommandText = """
            SELECT id, external_id, project, slot, task_type, scheduled_date, received_at, subject, body_markdown, is_read
            FROM task_messages
            ORDER BY received_at DESC, id DESC;
            """;

        await using var reader = await command.ExecuteReaderAsync(cancellationToken).ConfigureAwait(false);
        while (await reader.ReadAsync(cancellationToken).ConfigureAwait(false))
        {
            messages.Add(ReadMessage(reader));
        }

        return messages;
    }

    public async Task<IReadOnlySet<string>> GetExternalIdsAsync(CancellationToken cancellationToken = default)
    {
        var externalIds = new HashSet<string>(StringComparer.Ordinal);
        await using var connection = await OpenAsync(cancellationToken).ConfigureAwait(false);
        var command = connection.CreateCommand();
        command.CommandText = "SELECT external_id FROM task_messages;";

        await using var reader = await command.ExecuteReaderAsync(cancellationToken).ConfigureAwait(false);
        while (await reader.ReadAsync(cancellationToken).ConfigureAwait(false))
        {
            externalIds.Add(reader.GetString(0));
        }

        return externalIds;
    }

    public async Task MarkReadAsync(long id, CancellationToken cancellationToken = default)
    {
        await using var connection = await OpenAsync(cancellationToken).ConfigureAwait(false);
        var command = connection.CreateCommand();
        command.CommandText = "UPDATE task_messages SET is_read = 1 WHERE id = $id;";
        command.Parameters.AddWithValue("$id", id);
        await command.ExecuteNonQueryAsync(cancellationToken).ConfigureAwait(false);
    }

    private async Task<SqliteConnection> OpenAsync(CancellationToken cancellationToken)
    {
        var connection = new SqliteConnection(_connectionString);
        await connection.OpenAsync(cancellationToken).ConfigureAwait(false);
        return connection;
    }

    private static TaskMessage ReadMessage(SqliteDataReader reader) => new(
        reader.GetInt64(0),
        reader.GetString(1),
        reader.GetString(2),
        TimeOnly.ParseExact(reader.GetString(3), "HH:mm", CultureInfo.InvariantCulture),
        reader.GetString(4),
        DateOnly.ParseExact(reader.GetString(5), "yyyy-MM-dd", CultureInfo.InvariantCulture),
        DateTimeOffset.Parse(reader.GetString(6), CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind),
        reader.GetString(7),
        reader.GetString(8),
        reader.GetInt64(9) != 0);
}
