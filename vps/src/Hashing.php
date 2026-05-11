<?php
declare(strict_types=1);

namespace Tlinh;

use Normalizer;

// MD5 hashing helpers for `url_hash` and `title_hash`. Must produce byte-for-byte
// identical output to the Mac-side Python implementation in db.py (v1) so the
// VPS server-side hashes are interchangeable with the Mac-computed ones. See
// PLAN-v2.md §11 Task 7 acceptance: "url_hash matches Python output on identical
// canonical inputs (cross-test with Mac client)."
//
// Python reference (v1 db.py:168):
//   def normalize_title(title):
//     decomposed = unicodedata.normalize("NFD", title)
//     stripped   = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
//     no_punct   = re.sub(r"[^\w\s]", " ", stripped.lower(), flags=re.UNICODE)
//     return re.sub(r"\s+", " ", no_punct).strip()
//
// Python's `\w` under re.UNICODE matches Unicode letters + digits + underscore.
// PCRE's `\w` is ASCII-only even with /u; we therefore use `[\p{L}\p{N}_]` to
// match Python's behavior exactly. Tests in §13 row 1-rerun + Task 7 verify this.
final class Hashing
{
    public static function urlHash(string $canonicalUrl): string
    {
        return md5($canonicalUrl);
    }

    public static function titleHash(string $title): string
    {
        return md5(self::normalizeTitle($title));
    }

    public static function normalizeTitle(string $title): string
    {
        if ($title === '') {
            return '';
        }
        // 1. NFD decompose (intl ext: php-intl ships with stock Ubuntu PHP 8.5).
        $decomposed = Normalizer::normalize($title, Normalizer::FORM_D);
        if ($decomposed === false) {
            // If intl normalization fails (extremely rare), fall back to the
            // raw input so we still produce a stable hash.
            $decomposed = $title;
        }
        // 2. Strip combining marks (Unicode category Mn).
        $stripped = preg_replace('/\p{Mn}+/u', '', $decomposed);
        if ($stripped === null) {
            $stripped = $decomposed;
        }
        // 3. Lowercase (mb_* so non-ASCII still lowercases correctly).
        $lower = mb_strtolower($stripped, 'UTF-8');
        // 4. Replace non-word-or-space with a single space. We mirror Python's
        //    re.UNICODE \w via [\p{L}\p{N}_] explicitly (PCRE \w under /u is
        //    still ASCII-only).
        $noPunct = preg_replace('/[^\p{L}\p{N}_\s]/u', ' ', $lower);
        if ($noPunct === null) {
            $noPunct = $lower;
        }
        // 5. Collapse runs of whitespace to a single space, then trim.
        $collapsed = preg_replace('/\s+/u', ' ', $noPunct);
        if ($collapsed === null) {
            $collapsed = $noPunct;
        }
        return trim($collapsed);
    }
}
