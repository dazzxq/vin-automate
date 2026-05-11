<?php
declare(strict_types=1);

namespace Tlinh;

use PDO;

// Thin PDO wrapper. One connection per request (PHP-FPM worker reuses it).
final class Db
{
    private static ?PDO $pdo = null;

    // Connects (or returns the cached connection) and lets PDOException
    // propagate. The Router's normal-route flow catches at the boundary and
    // emits a JSON 500; the health route uses ping() to catch and return 503.
    public static function pdo(): PDO
    {
        if (self::$pdo === null) {
            $dsn = sprintf(
                'mysql:host=%s;dbname=%s;charset=utf8mb4',
                $_ENV['DB_HOST'],
                $_ENV['DB_NAME']
            );
            self::$pdo = new PDO($dsn, $_ENV['DB_USER'], $_ENV['DB_PASS'], [
                PDO::ATTR_ERRMODE            => PDO::ERRMODE_EXCEPTION,
                PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
                PDO::ATTR_EMULATE_PREPARES   => false,
                PDO::MYSQL_ATTR_INIT_COMMAND => "SET time_zone = '+00:00'",
            ]);
        }
        return self::$pdo;
    }

    // Quick health check used by GET /api/health. Returns true on a working
    // round-trip, false otherwise (no exception propagated). This is the only
    // call site that should catch connect failures — every other caller wants
    // the exception to bubble.
    public static function ping(): bool
    {
        try {
            self::pdo()->query('SELECT 1')->fetchColumn();
            return true;
        } catch (\Throwable $e) {
            error_log('tlinh.db_ping_failed: ' . $e->getMessage());
            return false;
        }
    }
}
