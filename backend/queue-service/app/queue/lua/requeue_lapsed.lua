-- End an admission whose selection deadline has passed, and put the buyer at the
-- back of the queue. Called when an admitted buyer polls or re-joins after
-- selection_expires_at (decided 2026-10-30). The frontend stops polling once the
-- buyer has a hold, so a buyer still polling past the deadline has none.
--
-- KEYS[1] config     queue:{eid}:config     (HASH, read: status)
-- KEYS[2] user_adm   queue:{eid}:user_adm   (HASH user_id -> admission_id)
-- KEYS[3] active     queue:{eid}:active     (ZSET admission_id -> expires ms)
-- KEYS[4] waiting    queue:{eid}:waiting    (ZSET user_id -> seq)
-- KEYS[5] seq        queue:{eid}:seq        (STRING counter)
-- KEYS[6] shuffled   queue:{eid}:shuffled   (STRING flag)
-- ARGV[1] user_id
-- ARGV[2] admission hash key prefix  queue:{eid}:adm:
-- ARGV[3] safety expiry (ms epoch) for keys this script creates
--
-- Reply: { outcome, admission_id }
--   'not_admitted'  the buyer holds no admission: nothing to do
--   'kept'          the selection deadline hasn't passed: nothing to do
--   'expired'       the admission deadline passed too: left to the sweep, like
--                   any ended admission (the buyer re-joins as usual)
--   'requeued'      admission removed, buyer appended to the waiting queue;
--                   the caller should admit the next buyer
--   'evicted'       admission removed, but the queue is CLOSED/CANCELLED, so
--                   the buyer is not requeued

local user_id = ARGV[1]
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
-- Never let a safety TTL land in the past (that would delete the key at once).
local expire_at = math.max(tonumber(ARGV[3]), now + 86400000)

local admission_id = redis.call('HGET', KEYS[2], user_id)
if not admission_id then
  return { 'not_admitted', '' }
end

local adm_key = ARGV[2] .. admission_id
local deadlines = redis.call('HMGET', adm_key, 'selection_expires_at', 'admission_expires_at')
local selection_expires, admission_expires = tonumber(deadlines[1]), tonumber(deadlines[2])
if not selection_expires then
  return { 'not_admitted', '' }  -- dangling user_adm entry; the sweep cleans up
end
if now <= selection_expires then
  return { 'kept', admission_id }
end
if admission_expires <= now then
  return { 'expired', admission_id }
end

redis.call('DEL', adm_key)
redis.call('HDEL', KEYS[2], user_id)
redis.call('ZREM', KEYS[3], admission_id)

local status = redis.call('HGET', KEYS[1], 'status')
if status == 'CLOSED' or status == 'CANCELLED' or redis.call('EXISTS', KEYS[6]) == 0 then
  return { 'evicted', admission_id }
end

local position = redis.call('INCR', KEYS[5])
redis.call('ZADD', KEYS[4], position, user_id)
redis.call('PEXPIREAT', KEYS[4], expire_at)
redis.call('PEXPIREAT', KEYS[5], expire_at)
return { 'requeued', admission_id }
