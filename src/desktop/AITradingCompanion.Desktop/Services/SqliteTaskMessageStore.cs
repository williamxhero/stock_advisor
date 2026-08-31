using System.Globalization;
using AITradingCompanion.Core.Models;
using Microsoft.Data.Sqlite;

namespace AITradingCompanion.Desktop.Services;

public sealed class SqliteTaskMessageStore : ITaskMessageStore
{
    private readonly string _databasePath;
    private readonly string _connectionString;

    public SqliteTaskMessageStore(AppPaths paths)
    {
        paths.EnsureDirectories();
        _databasePath = paths.DatabasePath;
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
        if (await HasLegacySchemaAsync(cancellationToken).ConfigureAwait(false))
        {
            var backupPath = _databasePath + ".pre-local-inbox-v1.bak";
            if (!File.Exists(backupPath))
            {
                File.Copy(_databasePath, backupPath);
            }
        }

        await using var connection = await OpenAsync(cancellationToken).ConfigureAwait(false);
        await using var transaction = await connection.BeginTransactionAsync(cancellationToken).ConfigureAwait(false);
        if (await HasColumnAsync(connection, "task_messages", "subject", cancellationToken).ConfigureAwait(false) &&
            !await HasColumnAsync(connection, "task_messages", "source", cancellationToken).ConfigureAwait(false))
        {
            await MigrateLegacyAsync(connection, (SqliteTransaction)transaction, cancellationToken).ConfigureAwait(false);
        }

        var command = connection.CreateCommand();
        command.Transaction = (SqliteTransaction)transaction;
        command.CommandText = SchemaSql;
        await command.ExecuteNonQueryAsync(cancellationToken).ConfigureAwait(false);
        await transaction.CommitAsync(cancellationToken).ConfigureAwait(false);
    }

    public async Task<TaskMessage?> AddAsync(IncomingTaskMessage incoming, CancellationToken cancellationToken = default)
    {
        await using var connection = await OpenAsync(cancellationToken).ConfigureAwait(false);
        await using var transaction = await connection.BeginTransactionAsync(cancellationToken).ConfigureAwait(false);
        var command = connection.CreateCommand();
        command.Transaction = (SqliteTransaction)transaction;
        command.CommandText = """
            INSERT OR IGNORE INTO task_messages
                (external_id, source, source_run_id, project, task_key, slot, task_type,
                 scheduled_date, scheduled_for, completed_at, received_at, status,
                 registry_id, protocol_id, summary, body_markdown, payload_json, content_sha256)
            VALUES
                ($external_id, $source, $source_run_id, $project, $task_key, $slot, $task_type,
                 $scheduled_date, $scheduled_for, $completed_at, $received_at, $status,
                 $registry_id, $protocol_id, $summary, $body_markdown, $payload_json, $content_sha256);
            SELECT changes();
            """;
        command.Parameters.AddWithValue("$external_id", incoming.ExternalId);
        command.Parameters.AddWithValue("$source", incoming.Source);
        command.Parameters.AddWithValue("$source_run_id", incoming.SourceRunId);
        command.Parameters.AddWithValue("$project", incoming.Project);
        command.Parameters.AddWithValue("$task_key", incoming.TaskKey);
        command.Parameters.AddWithValue("$slot", incoming.ScheduledFor.ToString("HH:mm", CultureInfo.InvariantCulture));
        command.Parameters.AddWithValue("$task_type", incoming.TaskType);
        command.Parameters.AddWithValue("$scheduled_date", incoming.ScheduledFor.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture));
        command.Parameters.AddWithValue("$scheduled_for", incoming.ScheduledFor.ToString("O", CultureInfo.InvariantCulture));
        command.Parameters.AddWithValue("$completed_at", incoming.CompletedAt.ToString("O", CultureInfo.InvariantCulture));
        command.Parameters.AddWithValue("$received_at", incoming.ReceivedAt.ToUniversalTime().ToString("O", CultureInfo.InvariantCulture));
        command.Parameters.AddWithValue("$status", StatusToStorage(incoming.Status));
        command.Parameters.AddWithValue("$registry_id", incoming.RegistryId);
        command.Parameters.AddWithValue("$protocol_id", incoming.ProtocolId);
        command.Parameters.AddWithValue("$summary", incoming.Summary);
        command.Parameters.AddWithValue("$body_markdown", incoming.BodyMarkdown);
        command.Parameters.AddWithValue("$payload_json", incoming.PayloadJson);
        command.Parameters.AddWithValue("$content_sha256", incoming.ContentSha256);
        var changes = Convert.ToInt32(await command.ExecuteScalarAsync(cancellationToken).ConfigureAwait(false), CultureInfo.InvariantCulture);
        if (changes == 0)
        {
            var existingCommand = connection.CreateCommand();
            existingCommand.Transaction = (SqliteTransaction)transaction;
            existingCommand.CommandText = "SELECT content_sha256 FROM task_messages WHERE source = $source AND external_id = $external_id;";
            existingCommand.Parameters.AddWithValue("$source", incoming.Source);
            existingCommand.Parameters.AddWithValue("$external_id", incoming.ExternalId);
            var existingHash = (string?)await existingCommand.ExecuteScalarAsync(cancellationToken).ConfigureAwait(false);
            await transaction.RollbackAsync(cancellationToken).ConfigureAwait(false);
            if (!string.Equals(existingHash, incoming.ContentSha256, StringComparison.Ordinal))
            {
                throw new InvalidDataException(
                    $"消息标识冲突：{incoming.Source}/{incoming.ExternalId} 已存在不同内容");
            }
            return null;
        }

        var idCommand = connection.CreateCommand();
        idCommand.Transaction = (SqliteTransaction)transaction;
        idCommand.CommandText = "SELECT last_insert_rowid();";
        var id = Convert.ToInt64(await idCommand.ExecuteScalarAsync(cancellationToken).ConfigureAwait(false), CultureInfo.InvariantCulture);
        var stateCommand = connection.CreateCommand();
        stateCommand.Transaction = (SqliteTransaction)transaction;
        stateCommand.CommandText = """
            INSERT INTO message_user_state(message_id, is_read, is_starred, is_archived, note, updated_at)
            VALUES ($id, 0, 0, 0, '', $updated_at);
            """;
        stateCommand.Parameters.AddWithValue("$id", id);
        stateCommand.Parameters.AddWithValue("$updated_at", DateTimeOffset.UtcNow.ToString("O", CultureInfo.InvariantCulture));
        await stateCommand.ExecuteNonQueryAsync(cancellationToken).ConfigureAwait(false);
        await transaction.CommitAsync(cancellationToken).ConfigureAwait(false);
        return CreateNewMessage(id, incoming);
    }

    public Task<IReadOnlyList<TaskMessage>> GetForDateAsync(DateOnly scheduledDate, CancellationToken cancellationToken = default) =>
        QueryAsync(
            "WHERE m.scheduled_date = $scheduled_date ORDER BY m.slot, m.completed_at DESC",
            command => command.Parameters.AddWithValue("$scheduled_date", scheduledDate.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture)),
            cancellationToken);

    public Task<IReadOnlyList<TaskMessage>> GetAllAsync(CancellationToken cancellationToken = default) =>
        QueryAsync("ORDER BY m.completed_at DESC, m.id DESC", null, cancellationToken);

    public Task SetReadAsync(long id, bool isRead, CancellationToken cancellationToken = default) =>
        UpdateStateAsync(id, "is_read", isRead ? 1 : 0, cancellationToken);

    public Task SetStarredAsync(long id, bool isStarred, CancellationToken cancellationToken = default) =>
        UpdateStateAsync(id, "is_starred", isStarred ? 1 : 0, cancellationToken);

    public Task SetArchivedAsync(long id, bool isArchived, CancellationToken cancellationToken = default) =>
        UpdateStateAsync(id, "is_archived", isArchived ? 1 : 0, cancellationToken);

    public async Task<int> RemoveGatewayCyclesAsync(IEnumerable<string> cycleIds, CancellationToken cancellationToken = default)
    {
        var ids = cycleIds.Where(id => !string.IsNullOrWhiteSpace(id)).Distinct(StringComparer.Ordinal).ToArray();
        if (ids.Length == 0) return 0;

        await using var connection = await OpenAsync(cancellationToken).ConfigureAwait(false);
        var command = connection.CreateCommand();
        var placeholders = new string[ids.Length];
        for (var index = 0; index < ids.Length; index++)
        {
            placeholders[index] = $"$cycle_id_{index}";
            command.Parameters.AddWithValue(placeholders[index], ids[index]);
        }
        command.CommandText = $"DELETE FROM task_messages WHERE source = 'gateway' AND source_run_id IN ({string.Join(", ", placeholders)});";
        return await command.ExecuteNonQueryAsync(cancellationToken).ConfigureAwait(false);
    }

    public async Task SaveNoteAsync(long id, string note, CancellationToken cancellationToken = default)
    {
        await using var connection = await OpenAsync(cancellationToken).ConfigureAwait(false);
        var command = connection.CreateCommand();
        command.CommandText = "UPDATE message_user_state SET note = $note, updated_at = $updated_at WHERE message_id = $id;";
        command.Parameters.AddWithValue("$note", note);
        command.Parameters.AddWithValue("$updated_at", DateTimeOffset.UtcNow.ToString("O", CultureInfo.InvariantCulture));
        command.Parameters.AddWithValue("$id", id);
        await command.ExecuteNonQueryAsync(cancellationToken).ConfigureAwait(false);
    }

    private async Task UpdateStateAsync(long id, string column, int value, CancellationToken cancellationToken)
    {
        await using var connection = await OpenAsync(cancellationToken).ConfigureAwait(false);
        var command = connection.CreateCommand();
        command.CommandText = $"UPDATE message_user_state SET {column} = $value, updated_at = $updated_at WHERE message_id = $id;";
        command.Parameters.AddWithValue("$value", value);
        command.Parameters.AddWithValue("$updated_at", DateTimeOffset.UtcNow.ToString("O", CultureInfo.InvariantCulture));
        command.Parameters.AddWithValue("$id", id);
        await command.ExecuteNonQueryAsync(cancellationToken).ConfigureAwait(false);
    }

    private async Task<IReadOnlyList<TaskMessage>> QueryAsync(
        string suffix,
        Action<SqliteCommand>? configure,
        CancellationToken cancellationToken)
    {
        var messages = new List<TaskMessage>();
        await using var connection = await OpenAsync(cancellationToken).ConfigureAwait(false);
        var command = connection.CreateCommand();
        command.CommandText = $"""
            SELECT m.id, m.external_id, m.source, m.source_run_id, m.project, m.task_key,
                   m.slot, m.task_type, m.scheduled_date, m.scheduled_for, m.completed_at,
                   m.received_at, m.status, m.registry_id, m.protocol_id, m.summary,
                   m.body_markdown, m.payload_json, m.content_sha256,
                   s.is_read, s.is_starred, s.is_archived, s.note
            FROM task_messages m
            JOIN message_user_state s ON s.message_id = m.id
            {suffix};
            """;
        configure?.Invoke(command);
        await using var reader = await command.ExecuteReaderAsync(cancellationToken).ConfigureAwait(false);
        while (await reader.ReadAsync(cancellationToken).ConfigureAwait(false))
        {
            messages.Add(ReadMessage(reader));
        }
        return messages;
    }

    private async Task<bool> HasLegacySchemaAsync(CancellationToken cancellationToken)
    {
        if (!File.Exists(_databasePath))
        {
            return false;
        }
        await using var connection = await OpenAsync(cancellationToken).ConfigureAwait(false);
        return await HasColumnAsync(connection, "task_messages", "subject", cancellationToken).ConfigureAwait(false) &&
            !await HasColumnAsync(connection, "task_messages", "source", cancellationToken).ConfigureAwait(false);
    }

    private static async Task<bool> HasColumnAsync(SqliteConnection connection, string table, string column, CancellationToken cancellationToken)
    {
        var command = connection.CreateCommand();
        command.CommandText = $"PRAGMA table_info({table});";
        await using var reader = await command.ExecuteReaderAsync(cancellationToken).ConfigureAwait(false);
        while (await reader.ReadAsync(cancellationToken).ConfigureAwait(false))
        {
            if (string.Equals(reader.GetString(1), column, StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }
        }
        return false;
    }

    private static async Task MigrateLegacyAsync(SqliteConnection connection, SqliteTransaction transaction, CancellationToken cancellationToken)
    {
        var command = connection.CreateCommand();
        command.Transaction = transaction;
        command.CommandText = "ALTER TABLE task_messages RENAME TO task_messages_legacy;" + SchemaSql + """
            INSERT INTO task_messages
                (id, external_id, source, source_run_id, project, task_key, slot, task_type,
                 scheduled_date, scheduled_for, completed_at, received_at, status,
                 registry_id, protocol_id, summary, body_markdown, payload_json, content_sha256)
            SELECT id, external_id, 'gmail-legacy', NULL, project, NULL, slot, task_type,
                   scheduled_date, scheduled_date || 'T' || slot || ':00+08:00', received_at,
                   received_at, 'succeeded', NULL, NULL, task_type, body_markdown, '{}', ''
            FROM task_messages_legacy;
            INSERT INTO message_user_state(message_id, is_read, is_starred, is_archived, note, updated_at)
            SELECT id, is_read, 0, 0, '', received_at FROM task_messages_legacy;
            DROP TABLE task_messages_legacy;
            """;
        await command.ExecuteNonQueryAsync(cancellationToken).ConfigureAwait(false);
    }

    private async Task<SqliteConnection> OpenAsync(CancellationToken cancellationToken)
    {
        var connection = new SqliteConnection(_connectionString);
        await connection.OpenAsync(cancellationToken).ConfigureAwait(false);
        var command = connection.CreateCommand();
        command.CommandText = "PRAGMA foreign_keys = ON; PRAGMA busy_timeout = 10000;";
        await command.ExecuteNonQueryAsync(cancellationToken).ConfigureAwait(false);
        return connection;
    }

    private static TaskMessage CreateNewMessage(long id, IncomingTaskMessage incoming) => new(
        id, incoming.ExternalId, incoming.Source, incoming.SourceRunId, incoming.Project, incoming.TaskKey,
        TimeOnly.FromDateTime(incoming.ScheduledFor.DateTime), incoming.TaskType,
        DateOnly.FromDateTime(incoming.ScheduledFor.DateTime), incoming.ScheduledFor, incoming.CompletedAt,
        incoming.ReceivedAt, incoming.Status, incoming.RegistryId, incoming.ProtocolId, incoming.Summary,
        incoming.BodyMarkdown, incoming.PayloadJson, incoming.ContentSha256, false, false, false, string.Empty);

    private static TaskMessage ReadMessage(SqliteDataReader reader) => new(
        reader.GetInt64(0), reader.GetString(1), reader.GetString(2), reader.IsDBNull(3) ? null : reader.GetString(3),
        reader.GetString(4), reader.IsDBNull(5) ? null : reader.GetString(5),
        TimeOnly.ParseExact(reader.GetString(6), "HH:mm", CultureInfo.InvariantCulture), reader.GetString(7),
        DateOnly.ParseExact(reader.GetString(8), "yyyy-MM-dd", CultureInfo.InvariantCulture),
        DateTimeOffset.Parse(reader.GetString(9), CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind),
        DateTimeOffset.Parse(reader.GetString(10), CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind),
        DateTimeOffset.Parse(reader.GetString(11), CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind),
        StatusFromStorage(reader.GetString(12)), reader.IsDBNull(13) ? null : reader.GetString(13),
        reader.IsDBNull(14) ? null : reader.GetString(14), reader.GetString(15), reader.GetString(16),
        reader.GetString(17), reader.GetString(18), reader.GetInt64(19) != 0, reader.GetInt64(20) != 0,
        reader.GetInt64(21) != 0, reader.GetString(22));

    private static string StatusToStorage(TaskMessageStatus status) => status switch
    {
        TaskMessageStatus.Succeeded => "succeeded",
        TaskMessageStatus.Skipped => "skipped",
        TaskMessageStatus.Failed => "failed",
        _ => throw new ArgumentOutOfRangeException(nameof(status))
    };

    private static TaskMessageStatus StatusFromStorage(string status) => status switch
    {
        "succeeded" => TaskMessageStatus.Succeeded,
        "skipped" => TaskMessageStatus.Skipped,
        "failed" => TaskMessageStatus.Failed,
        _ => throw new InvalidDataException($"Unknown message status: {status}")
    };

    private const string SchemaSql = """
        CREATE TABLE IF NOT EXISTS task_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            external_id TEXT NOT NULL,
            source TEXT NOT NULL,
            source_run_id TEXT,
            project TEXT NOT NULL,
            task_key TEXT,
            slot TEXT NOT NULL,
            task_type TEXT NOT NULL,
            scheduled_date TEXT NOT NULL,
            scheduled_for TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            received_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('succeeded', 'skipped', 'failed')),
            registry_id TEXT,
            protocol_id TEXT,
            summary TEXT NOT NULL,
            body_markdown TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            UNIQUE (source, external_id)
        );
        CREATE TABLE IF NOT EXISTS message_user_state (
            message_id INTEGER PRIMARY KEY REFERENCES task_messages(id) ON DELETE CASCADE,
            is_read INTEGER NOT NULL DEFAULT 0,
            is_starred INTEGER NOT NULL DEFAULT 0,
            is_archived INTEGER NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_task_messages_scheduled_date ON task_messages(scheduled_date, task_key, completed_at);
        CREATE INDEX IF NOT EXISTS ix_task_messages_completed_at ON task_messages(completed_at DESC);
        PRAGMA user_version = 1;
        """;
}
