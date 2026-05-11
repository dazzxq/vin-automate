<?php
declare(strict_types=1);

namespace Tlinh;

// Tiny JSON helpers used by every route handler.
final class Json
{
    // Parse the request body as JSON. Returns an associative array on success.
    // On parse failure, emits a 400 JSON response and exits.
    public static function body(): array
    {
        $raw = file_get_contents('php://input');
        if ($raw === false || $raw === '') {
            self::out(400, ['error' => 'bad_request', 'detail' => 'empty body']);
        }
        try {
            $parsed = json_decode($raw, true, 32, JSON_THROW_ON_ERROR);
        } catch (\JsonException $e) {
            self::out(400, ['error' => 'bad_json', 'detail' => $e->getMessage()]);
        }
        if (!is_array($parsed)) {
            self::out(400, ['error' => 'bad_json', 'detail' => 'expected object']);
        }
        /** @var array $parsed */
        return $parsed;
    }

    // Send a JSON response and end the request.
    public static function out(int $status, array $payload, array $extraHeaders = []): never
    {
        http_response_code($status);
        // Content-Type already set by Router; re-set defensively in case a
        // handler is called outside that flow (e.g. tests).
        if (!headers_sent()) {
            header('Content-Type: application/json');
            foreach ($extraHeaders as $k => $v) {
                header("$k: $v");
            }
        }
        echo json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
        exit;
    }
}
