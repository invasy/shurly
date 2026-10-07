// k6 load test for shurly.
//
// Mirrors scripts/load_test.py: ~95% redirect reads, ~5% URL creates.
//
//   export SHURLY_HOST=http://localhost:8000
//   export SHURLY_SEED_CODES="abc,def,ghi"   # existing short codes to redirect
//   export SHURLY_API_KEY="shurly-a-..."     # needed only for the create flow
//   k6 run scripts/load_test.js
//
// Tune VUS / duration with the env below or --vus/--duration flags.

import http from 'k6/http';
import { check, sleep, group } from 'k6';
import { Counter } from 'k6/metrics';

const HOST = __ENV.SHURLY_HOST || 'http://localhost:8000';
const SEED = (
  (__ENV.SHURLY_SEED_CODES || 'seed1,seed2,seed3').split(',').map((s) => s.trim()).filter(Boolean)
);
const API_KEY = __ENV.SHURLY_API_KEY || '';

const VUS = parseInt(__ENV.SHURLY_VUS || '500', 10);
const DURATION = __ENV.SHURLY_DURATION || '5m';

// Redirects served (counts 3xx responses).
const redirects = new Counter('shurly_redirects');

export const options = {
  vus: VUS,
  duration: DURATION,
  // Redirects must stay under 50ms; flag anything over 50ms.
  thresholds: {
    http_req_duration: ['p(99)<50'],
  },
};

function randomSeed() {
  return SEED[Math.floor(Math.random() * SEED.length)];
}

export function setup() {
  console.log(`target=${HOST} seed=${SEED.length} codes, api_key=${API_KEY ? 'set' : 'absent'}`);
}

export default function () {
  if (Math.random() < 0.95) {
    group('redirect (read)', function () {
      if (!SEED.length) return;
      const res = http.get(`${HOST}/${randomSeed()}`);
      redirects.add(1);
      check(res, {
        'redirect is 3xx': (r) => r.status >= 300 && r.status < 400,
        'has location': (r) => !!r.headers['Location'],
      });
    });
  } else {
    group('create (write)', function () {
      if (!API_KEY) return;
      const res = http.post(
        `${HOST}/api/v1/urls`,
        JSON.stringify({ url: `https://example.com/load/${Date.now()}-${Math.random()}` }),
        {
          headers: { 'X-API-Key': API_KEY, 'Content-Type': 'application/json' },
        }
      );
      check(res, { 'create is 201': (r) => r.status === 201 });
    });
  }
  sleep(0.05 + Math.random() * 0.2);
}
