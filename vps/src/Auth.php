<?php
declare(strict_types=1);

namespace Tlinh;

// Bearer-token check, constant-time compare via hash_equals (per PLAN-v2 §7.1).
// Health endpoint is exempt — it must be reachable without auth for monitoring.
final class Auth
{
    public static function requireBearer(): void
    {
        $expected = (string)($_ENV['API_TOKEN'] ?? '');
        $header   = self::headerAuthorization();

        if ($header === null || strncasecmp($header, 'Bearer ', 7) !== 0) {
            error_log('tlinh.auth_rejected: missing_bearer');
            self::reject();
        }
        $presented = substr($header, 7);

        // hash_equals avoids timing attacks; both sides MUST be the same length
        // or it returns false even for matching prefixes — that's the point.
        if (!hash_equals($expected, $presented)) {
            error_log('tlinh.auth_rejected: bad_bearer');
            self::reject();
        }
    }

    private static function headerAuthorization(): ?string
    {
        // nginx vhost passes Authorization via fastcgi_param HTTP_AUTHORIZATION
        // (see §10 Step 6). PHP also exposes it via $_SERVER['HTTP_AUTHORIZATION']
        // and getallheaders(); try them in order.
        if (!empty($_SERVER['HTTP_AUTHORIZATION'])) {
            return $_SERVER['HTTP_AUTHORIZATION'];
        }
        if (function_exists('getallheaders')) {
            $h = getallheaders();
            foreach ($h as $k => $v) {
                if (strcasecmp($k, 'Authorization') === 0) {
                    return $v;
                }
            }
        }
        return null;
    }

    private static function reject(): never
    {
        http_response_code(401);
        header('Content-Type: application/json');
        echo json_encode(['error' => 'unauthorized']);
        exit;
    }
}
