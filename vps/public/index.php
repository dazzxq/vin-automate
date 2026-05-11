<?php
declare(strict_types=1);

// Front controller for the tlinh-news v2 API.
// All requests under https://tlinh.duyet.vn/ enter here via the nginx vhost.
// See PLAN-v2.md §4 for the routing contract and §5 for the layout rationale.

require __DIR__ . '/../src/bootstrap.php';

use Tlinh\Router;

$router = new Router();
$router->dispatch();
