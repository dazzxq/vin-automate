<?php
declare(strict_types=1);

// Loads env from shared/.env, sets up the PSR-0-ish "Tlinh\*" autoloader,
// validates required env values, and configures basic error handling.

namespace { // global namespace for the autoloader closure

    // Resolve env path relative to the release directory. The deploy layout is:
    //   /var/www/tlinh/tlinh.duyet.vn/
    //     ├── releases/v1/src/bootstrap.php  ← this file
    //     ├── current  → releases/v1         (symlink)
    //     └── shared/.env                     ← what we want
    //
    // PHP canonicalizes symlinks in __DIR__, so __DIR__ resolves to the
    // releases/v1/src path even when reached via /current/src/. We must
    // therefore walk 3 levels up (src → v1 → releases → tlinh.duyet.vn)
    // not 2. Fall back to the 2-level path so any future layout that
    // skips the release-versioning layer still works.
    $candidates = [dirname(__DIR__, 3) . '/shared/.env', dirname(__DIR__, 2) . '/shared/.env'];
    $envPath = null;
    foreach ($candidates as $c) {
        if (is_readable($c)) { $envPath = $c; break; }
    }
    if ($envPath === null) {
        $envPath = $candidates[0]; // for the error message below
    }
    if (!is_readable($envPath)) {
        // shared/.env is created by deploy.sh Step 5. If we got here without
        // it, the deploy is broken — fail fast instead of leaking PHP errors.
        http_response_code(500);
        header('Content-Type: application/json');
        echo json_encode(['error' => 'env_missing']);
        exit;
    }
    // .env is `KEY=VALUE` newline-delimited (no quoting/expansion). Anything
    // fancier would require a parser; deploy.sh only writes plain lines.
    foreach (file($envPath, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) as $line) {
        if ($line === '' || $line[0] === '#') {
            continue;
        }
        $eq = strpos($line, '=');
        if ($eq === false) {
            continue;
        }
        $k = trim(substr($line, 0, $eq));
        $v = trim(substr($line, $eq + 1));
        $_ENV[$k] = $v;
        putenv("$k=$v");
    }

    // Required keys — refuse to boot without them so misconfigured deploys
    // don't silently return 500s downstream.
    $required = [
        'DB_HOST', 'DB_NAME', 'DB_USER', 'DB_PASS',
        'API_TOKEN', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID',
        'TITLE_DEDUP_WINDOW_HOURS', 'MAX_RETRIES', 'LOCK_TTL_SECONDS',
        'NOTIFY_CLAIM_TTL_SECONDS', 'NOTIFY_CLAIM_HEARTBEAT_SECONDS',
        'NOTIFY_TELEGRAM_RETRY_AFTER_CAP_SECONDS',
        'NOTIFY_TELEGRAM_REQUEST_TIMEOUT_SECONDS',
        'MIN_SCORE_TO_NOTIFY', 'MAX_ARTICLES_PER_LIST',
    ];
    foreach ($required as $k) {
        if (($_ENV[$k] ?? '') === '') {
            http_response_code(500);
            header('Content-Type: application/json');
            echo json_encode(['error' => 'env_missing_key', 'key' => $k]);
            exit;
        }
    }

    // Boot-time sanity check for the notify-claim invariant (per PLAN-v2 §4
    // POST /api/notify "Claim TTL sizing"):
    //   heartbeat * 3 ≤ TTL  AND  phase2_budget + 60 ≤ TTL.
    $ttl  = (int)$_ENV['NOTIFY_CLAIM_TTL_SECONDS'];
    $hb   = (int)$_ENV['NOTIFY_CLAIM_HEARTBEAT_SECONDS'];
    $cap  = (int)$_ENV['NOTIFY_TELEGRAM_RETRY_AFTER_CAP_SECONDS'];
    $rto  = (int)$_ENV['NOTIFY_TELEGRAM_REQUEST_TIMEOUT_SECONDS'];
    $phase2Budget = 31 + $cap + 5 * $rto;
    if ($ttl < 3 * $hb || $ttl < $phase2Budget + 60) {
        http_response_code(500);
        header('Content-Type: application/json');
        echo json_encode([
            'error' => 'notify_claim_config_invalid',
            'detail' => '[fatal] NOTIFY_CLAIM_TTL_SECONDS must be ≥ 3 × NOTIFY_CLAIM_HEARTBEAT_SECONDS AND ≥ NOTIFY_PHASE2_BUDGET_SECONDS + 60',
            'ttl' => $ttl, 'heartbeat' => $hb, 'phase2_budget' => $phase2Budget,
        ]);
        exit;
    }

    // Autoloader for Tlinh\* classes. The plan (§5) ships route handlers
    // under `vps/src/routes/<lowercase>.php`, so the `Tlinh\Routes\Foo`
    // namespace is special-cased to that lowercase directory + filename.
    // Linux filesystems are case-sensitive, so this mapping must be exact.
    //   Tlinh\Db                 -> vps/src/Db.php
    //   Tlinh\Routes\Articles    -> vps/src/routes/articles.php
    spl_autoload_register(function (string $class): void {
        $prefix = 'Tlinh\\';
        if (strncmp($class, $prefix, strlen($prefix)) !== 0) {
            return;
        }
        $rel = substr($class, strlen($prefix));
        if (strncmp($rel, 'Routes\\', 7) === 0) {
            $name = substr($rel, 7);
            $file = __DIR__ . '/routes/' . strtolower($name) . '.php';
        } else {
            $file = __DIR__ . '/' . str_replace('\\', '/', $rel) . '.php';
        }
        if (is_file($file)) {
            require $file;
        }
    });
}
