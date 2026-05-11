<?php
declare(strict_types=1);

namespace Tlinh\Routes;

use Tlinh\Db;
use Tlinh\Json;

// PATCH /api/articles/{id}/brainstorm — overwrite-friendly (per §4: "brainstorm
// can re-run", no CAS). Validates exactly 5 ideas with the 5 required fields.
final class Brainstorm
{
    public static function update(string $id): void
    {
        $body  = Json::body();
        $ideas = $body['ideas'] ?? null;
        if (!is_array($ideas) || count($ideas) !== 5) {
            Json::out(400, ['error' => 'bad_request', 'message' => 'ideas must be array of exactly 5 items']);
        }
        foreach ($ideas as $i => $idea) {
            if (!is_array($idea)) {
                Json::out(400, ['error' => 'bad_request', 'message' => "ideas[$i] must be object"]);
            }
            foreach (['title', 'angle', 'format', 'difficulty'] as $k) {
                if (!isset($idea[$k]) || !is_string($idea[$k]) || $idea[$k] === '') {
                    Json::out(400, ['error' => 'bad_request', 'message' => "ideas[$i].$k missing or not a string"]);
                }
            }
            $vp = $idea['viral_potential'] ?? null;
            if (!is_int($vp) || $vp < 1 || $vp > 5) {
                Json::out(400, ['error' => 'bad_request', 'message' => "ideas[$i].viral_potential must be int 1..5"]);
            }
        }

        $pdo  = Db::pdo();
        $stmt = $pdo->prepare(
            'UPDATE articles
                SET brainstormed_at = NOW(),
                    ideas = :ideas
              WHERE id = :id'
        );
        $stmt->execute([
            ':ideas' => json_encode($ideas, JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR),
            ':id'    => (int)$id,
        ]);
        if ($stmt->rowCount() === 0) {
            // Row didn't exist OR ideas+brainstormed_at unchanged (same JSON +
            // same NOW second). Verify with a SELECT.
            $exists = $pdo->prepare('SELECT id, brainstormed_at FROM articles WHERE id = ?');
            $exists->execute([(int)$id]);
            $row = $exists->fetch();
            if (!$row) {
                Json::out(404, ['error' => 'not_found']);
            }
            Json::out(200, [
                'id'              => (int)$id,
                'updated'         => true,
                'brainstormed_at' => $row['brainstormed_at'],
            ]);
        }
        $stmt = $pdo->prepare('SELECT brainstormed_at FROM articles WHERE id = ?');
        $stmt->execute([(int)$id]);
        Json::out(200, [
            'id'              => (int)$id,
            'updated'         => true,
            'brainstormed_at' => $stmt->fetchColumn(),
        ]);
    }
}
