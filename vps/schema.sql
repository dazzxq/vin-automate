-- vin-automate v2 — VPS DB schema (MariaDB 10.11+)
--
-- Idempotent re-application: deploy.sh runs this via
--   mariadb -u tlinh -p"$DB_PASS" tlinh_news < schema.sql
-- Only difference vs PLAN-v2.md §3 is `CREATE TABLE IF NOT EXISTS` so reruns
-- are safe. The database + user are created by the root-mariadb block in
-- deploy.sh Step 4 (not by this file, because the tlinh user has no CREATE
-- DATABASE / CREATE USER grants).

USE tlinh_news;

CREATE TABLE IF NOT EXISTS articles (
  id              BIGINT PRIMARY KEY AUTO_INCREMENT,
  url             TEXT NOT NULL,
  url_hash        CHAR(32) NOT NULL,
  canonical_url   TEXT,
  title           TEXT,
  title_hash      CHAR(32),
  source          VARCHAR(255),
  content         MEDIUMTEXT,
  published_at    DATETIME NULL,
  crawled_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  extracted_at    DATETIME NULL,
  scored_at       DATETIME NULL,
  -- 2-phase notify (per Codex ISSUE-1): claim BEFORE external send, finalize AFTER.
  notify_claimed_at  DATETIME NULL,                -- set on Phase 1 atomic claim
  notify_claim_owner CHAR(32) NULL,                -- UUID generated server-side at claim
  notified_at     DATETIME NULL,                   -- set on Phase 2 after Telegram 200 OK
  brainstormed_at DATETIME NULL,
  failed_at       DATETIME NULL,
  score           TINYINT UNSIGNED NULL,           -- 1..5 (NULL = unscored)
  score_reason    TEXT,
  ideas           JSON,
  telegram_msg_id BIGINT NULL,
  last_error      TEXT,
  retry_count     INT UNSIGNED NOT NULL DEFAULT 0,
  final_state     VARCHAR(20) NULL,                -- 'discarded' | 'archived' | NULL
  UNIQUE KEY uq_url_hash (url_hash),
  KEY idx_extracted_at (extracted_at),
  KEY idx_scored_at (scored_at),
  KEY idx_notify_claimed_at (notify_claimed_at),
  KEY idx_notified_at (notified_at),
  KEY idx_brainstormed_at (brainstormed_at),
  KEY idx_title_hash (title_hash),
  KEY idx_score (score),
  KEY idx_final_state (final_state)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS locks (
  name        VARCHAR(64) PRIMARY KEY,
  owner_id    CHAR(32) NOT NULL,
  acquired_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at  DATETIME NOT NULL,
  owner_pid   INT NULL,
  KEY idx_expires (expires_at)
) ENGINE=InnoDB;
