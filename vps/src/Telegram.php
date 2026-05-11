<?php
declare(strict_types=1);

namespace Tlinh;

// Thin Telegram Bot API client used by the Notify route. Implements the
// retry/backoff/Retry-After-cap contract from PLAN-v2 §9.
//
// Per request timeout = NOTIFY_TELEGRAM_REQUEST_TIMEOUT_SECONDS (30s default).
// 5xx backoff = 1s, 2s, 4s, 8s, 16s (5 attempts).
// Retry-After honored up to NOTIFY_TELEGRAM_RETRY_AFTER_CAP_SECONDS (60s).
//
// During any sleep, this class invokes the optional $heartbeat callable to
// refresh the notify-claim every NOTIFY_CLAIM_HEARTBEAT_SECONDS. If the
// heartbeat callable returns false (claim stolen / notified_at set), the
// helper aborts immediately and returns ['aborted' => true].
final class Telegram
{
    public static function sendMessage(string $html, ?callable $heartbeat = null): array
    {
        $token   = (string)$_ENV['TELEGRAM_BOT_TOKEN'];
        $chatId  = (string)$_ENV['TELEGRAM_CHAT_ID'];
        $timeout = (int)$_ENV['NOTIFY_TELEGRAM_REQUEST_TIMEOUT_SECONDS'];
        $cap     = (int)$_ENV['NOTIFY_TELEGRAM_RETRY_AFTER_CAP_SECONDS'];
        $hbEvery = (int)$_ENV['NOTIFY_CLAIM_HEARTBEAT_SECONDS'];

        $url = "https://api.telegram.org/bot{$token}/sendMessage";
        $payload = [
            'chat_id'                  => $chatId,
            'text'                     => $html,
            'parse_mode'               => 'HTML',
            'disable_web_page_preview' => true,
        ];

        $backoffSchedule = [1, 2, 4, 8, 16];
        $attempts = count($backoffSchedule) + 1;          // last attempt with no following sleep
        $lastResp = ['ok' => false, 'status' => 0, 'body' => null];

        for ($i = 0; $i < $attempts; $i++) {
            // Heartbeat before each attempt (esp. after a sleep).
            if ($heartbeat !== null && $heartbeat() === false) {
                return ['aborted' => true, 'reason' => 'claim_stolen_during_retry'];
            }

            $resp = self::httpPost($url, $payload, $timeout, $heartbeat, $hbEvery);
            $lastResp = $resp;

            // The curl progress callback aborts the transfer as soon as the
            // heartbeat detects a stolen claim. Treat that as an early abort
            // identical to the post-sleep heartbeat path.
            if (($resp['aborted_by_heartbeat'] ?? false) === true) {
                return ['aborted' => true, 'reason' => 'claim_stolen_during_request'];
            }

            if ($resp['status'] === 200) {
                $body = json_decode((string)$resp['body'], true);
                $msgId = $body['result']['message_id'] ?? null;
                return ['ok' => true, 'message_id' => $msgId];
            }

            // 429: honor Retry-After up to cap. Over cap → abort.
            if ($resp['status'] === 429) {
                $retryAfter = isset($resp['headers']['retry-after'])
                    ? (int)$resp['headers']['retry-after']
                    : 1;
                if ($retryAfter > $cap) {
                    error_log("notify.retry_after_exceeded_cap: retry_after={$retryAfter} cap={$cap}");
                    return ['ok' => false, 'status' => 429, 'body' => $resp['body'], 'reason' => 'retry_after_exceeded_cap', 'retry_after' => $retryAfter];
                }
                if ($i === $attempts - 1) { break; }
                if (!self::heartbeatedSleep($retryAfter, $hbEvery, $heartbeat)) {
                    return ['aborted' => true, 'reason' => 'claim_stolen_during_retry'];
                }
                continue;
            }

            // Transport failures (status=0 from a curl_exec failure) AND 5xx:
            // both transient under §9 retry budget. The previous version
            // misclassified status=0 as permanent_4xx and never retried.
            if ($resp['status'] === 0 || ($resp['status'] >= 500 && $resp['status'] < 600)) {
                if ($i === $attempts - 1) { break; }
                $sleep = $backoffSchedule[$i] ?? 16;
                if (!self::heartbeatedSleep($sleep, $hbEvery, $heartbeat)) {
                    return ['aborted' => true, 'reason' => 'claim_stolen_during_retry'];
                }
                continue;
            }

            // 4xx other than 429: permanent failure, no retry.
            return ['ok' => false, 'status' => $resp['status'], 'body' => $resp['body'], 'reason' => 'permanent_4xx'];
        }

        // Exhausted retries on transient errors.
        return ['ok' => false, 'status' => $lastResp['status'], 'body' => $lastResp['body'], 'reason' => 'retries_exhausted'];
    }

    private static function httpPost(string $url, array $payload, int $timeoutSeconds, ?callable $heartbeat, int $hbEvery): array
    {
        $ch = curl_init($url);
        $body = json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR);

        // PHP-FPM is single-threaded; we cannot fork a real background task,
        // so we tick heartbeats from inside the curl progress callback. This
        // fires several times per second during the request and lets us
        // refresh the notify claim every NOTIFY_CLAIM_HEARTBEAT_SECONDS even
        // while curl_exec is blocked on the network. Returning non-zero from
        // the progress callback aborts the transfer immediately, which we use
        // to bail out as soon as the heartbeat detects a stolen claim.
        $headerLines = [];
        $lastHb      = microtime(true);
        $aborted     = false;
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_POST           => true,
            CURLOPT_POSTFIELDS     => $body,
            CURLOPT_HTTPHEADER     => ['Content-Type: application/json'],
            CURLOPT_TIMEOUT        => $timeoutSeconds,
            CURLOPT_CONNECTTIMEOUT => max(5, (int)floor($timeoutSeconds / 4)),
            CURLOPT_NOPROGRESS     => false,
            CURLOPT_HEADERFUNCTION => function ($ch, $line) use (&$headerLines) {
                $trim = rtrim($line);
                if ($trim !== '' && str_contains($trim, ':')) {
                    [$k, $v] = explode(':', $trim, 2);
                    $headerLines[strtolower(trim($k))] = trim($v);
                }
                return strlen($line);
            },
            CURLOPT_PROGRESSFUNCTION => function ($ch, $dlSize, $dl, $ulSize, $ul) use ($heartbeat, $hbEvery, &$lastHb, &$aborted) {
                if ($heartbeat === null || $hbEvery <= 0) {
                    return 0;
                }
                $now = microtime(true);
                if ($now - $lastHb >= $hbEvery) {
                    $lastHb = $now;
                    if ($heartbeat() === false) {
                        $aborted = true;
                        return 1; // non-zero aborts the curl transfer
                    }
                }
                return 0;
            },
        ]);
        $respBody = curl_exec($ch);
        if ($respBody === false) {
            $err = curl_error($ch);
            curl_close($ch);
            if ($aborted) {
                return ['status' => 0, 'body' => null, 'headers' => [], 'aborted_by_heartbeat' => true];
            }
            return ['status' => 0, 'body' => null, 'headers' => [], 'curl_error' => $err];
        }
        $status = (int)curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
        curl_close($ch);
        return ['status' => $status, 'body' => $respBody, 'headers' => $headerLines];
    }

    // Sleep in chunks of $hbEvery seconds, invoking $heartbeat between chunks.
    // Returns false if heartbeat reported the claim was stolen; true otherwise.
    private static function heartbeatedSleep(int $totalSeconds, int $hbEvery, ?callable $heartbeat): bool
    {
        if ($totalSeconds <= 0) {
            return true;
        }
        if ($heartbeat === null || $hbEvery <= 0 || $hbEvery >= $totalSeconds) {
            sleep($totalSeconds);
            return true;
        }
        $remaining = $totalSeconds;
        while ($remaining > 0) {
            $chunk = min($hbEvery, $remaining);
            sleep($chunk);
            $remaining -= $chunk;
            if ($remaining > 0) {
                $ok = $heartbeat();
                if ($ok === false) {
                    return false;
                }
            }
        }
        return true;
    }
}
