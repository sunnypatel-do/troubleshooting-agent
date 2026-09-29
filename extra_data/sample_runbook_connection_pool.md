# Runbook: Database connection pool exhaustion (sample, replace with your own)

Symptoms: checkout or order requests time out, latency climbs on the calling service, errors such as
"timeout acquiring connection". Upstream services may show critical latency alerts while the real fault is
saturation at the data layer.

Checks:
1. Compare active connections vs pool max for the service that owns the pool.
2. Look for a recent deploy or config change that lowered pool size or added slow queries.
3. Check traces for time spent waiting before the DB span starts.

Mitigation: raise pool size temporarily, roll back the change, or shed load. Escalate to the data platform team.
