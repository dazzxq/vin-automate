<?php
declare(strict_types=1);

namespace Tlinh;

// Tiny dispatcher: matches METHOD + path, calls a route handler. Each route
// handler lives in vps/src/routes/<Group>.php and exposes static callables.
//
// We intentionally do NOT pull in a 3rd-party router — the endpoint surface is
// small (< 20 routes), and a hand-written matcher avoids vendor dependencies.
final class Router
{
    public function dispatch(): void
    {
        header('Content-Type: application/json');

        $method = $_SERVER['REQUEST_METHOD'] ?? 'GET';
        $uri    = parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH) ?: '/';

        // GET /api/health — no auth, used by monitoring and `deploy.sh` Step 7.
        if ($method === 'GET' && $uri === '/api/health') {
            $this->routeHealth();
            return;
        }

        // Everything below requires a bearer token.
        Auth::requireBearer();

        try {
            $this->route($method, $uri);
        } catch (\PDOException $e) {
            // DB hiccup mid-request. Log details server-side; respond with a
            // generic 500 so we don't leak SQL state to callers.
            error_log('tlinh.db_error: ' . $e->getMessage());
            http_response_code(500);
            echo json_encode(['error' => 'db_error']);
        } catch (\Throwable $e) {
            error_log('tlinh.unhandled: ' . $e::class . ' ' . $e->getMessage());
            http_response_code(500);
            echo json_encode(['error' => 'internal_error']);
        }
    }

    private function route(string $method, string $uri): void
    {

        // Routes registered as (method, regex, [Class, method]). The class is
        // resolved by the bootstrap autoloader (Tlinh\Routes\<Class>).
        $routes = [
            // Articles
            ['POST',  '#^/api/articles$#',                          ['Tlinh\\Routes\\Articles', 'create']],
            ['GET',   '#^/api/articles$#',                          ['Tlinh\\Routes\\Articles', 'list']],
            ['GET',   '#^/api/articles/(\d+)$#',                    ['Tlinh\\Routes\\Articles', 'show']],
            ['PATCH', '#^/api/articles/(\d+)/extract$#',            ['Tlinh\\Routes\\Articles', 'extract']],
            ['PATCH', '#^/api/articles/(\d+)/score$#',              ['Tlinh\\Routes\\Score',    'update']],
            ['PATCH', '#^/api/articles/(\d+)/brainstorm$#',         ['Tlinh\\Routes\\Brainstorm','update']],
            ['POST',  '#^/api/articles/(\d+)/fail$#',               ['Tlinh\\Routes\\Fail',     'record']],
            // Notify (2-phase claim)
            ['POST',  '#^/api/notify/(\d+)$#',                      ['Tlinh\\Routes\\Notify',   'trigger']],
            // Locks
            ['POST',  '#^/api/lock/([\w\-]+)/acquire$#',            ['Tlinh\\Routes\\Lock',     'acquire']],
            ['POST',  '#^/api/lock/([\w\-]+)/heartbeat$#',          ['Tlinh\\Routes\\Lock',     'heartbeat']],
            ['DELETE','#^/api/lock/([\w\-]+)$#',                    ['Tlinh\\Routes\\Lock',     'release']],
            // Admin (nginx allow 127.0.0.1; deny all on this path — see §10 Step 6)
            ['POST',  '#^/api/admin/inject-test$#',                 ['Tlinh\\Routes\\Admin',    'injectTest']],
        ];

        foreach ($routes as [$m, $pattern, $handler]) {
            if ($m !== $method) {
                continue;
            }
            if (preg_match($pattern, $uri, $matches) === 1) {
                array_shift($matches); // drop full match, keep groups
                [$class, $func] = $handler;
                $class::$func(...$matches);
                return;
            }
        }

        http_response_code(404);
        echo json_encode(['error' => 'not_found', 'method' => $method, 'path' => $uri]);
    }

    private function routeHealth(): void
    {
        $dbOk = Db::ping();
        if (!$dbOk) {
            http_response_code(503);
            echo json_encode(['status' => 'degraded', 'db' => 'disconnected']);
            return;
        }
        // VERSION file is written by deploy.sh with the git SHA (release/vN).
        // Falls back to literal "v2" when running from a checkout without it.
        $versionFile = __DIR__ . '/../VERSION';
        $version = is_file($versionFile) ? trim((string)file_get_contents($versionFile)) : 'v2';
        echo json_encode([
            'status'  => 'ok',
            'db'      => 'connected',
            'version' => $version,
        ]);
    }
}
