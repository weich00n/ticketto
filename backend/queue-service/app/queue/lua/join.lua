-- Join a buyer to an event's queue, or return their existing state.
--
-- KEYS[1] config     queue:{eid}:config     (HASH, read: status)
-- KEYS[2] user_adm   queue:{eid}:user_adm   (HASH user_id -> admission_id)
-- KEYS[3] prequeue   queue:{eid}:prequeue   (SET)
-- KEYS[4] waiting    queue:{eid}:waiting    (ZSET user_id -> seq)
-- KEYS[5] seq        queue:{eid}:seq        (STRING counter)
-- KEYS[6] shuffled   queue:{eid}:shuffled   (STRING flag)
-- KEYS[7] active     queue:{eid}:active     (ZSET admission_id -> expires ms)
-- ARGV[1] user_id
-- ARGV[2] safety expiry (ms epoch) for keys this script creates
-- ARGV[3] admission hash key prefix  queue:{eid}:adm:
--
-- Reply: { outcome, detail, swept_admission_id }
--   outcome  'admitted' (detail = admission_id) | 'closed' (detail = status)
--            | 'waiting' | 'created'
--   swept_admission_id  an expired admission this call removed, else ''
--
-- Order of checks: an unexpired admission wins even on a CLOSED queue (the
-- buyer keeps it until order or deadline); then terminal status refuses new
-- entries; then existing entries are returned unchanged; then a new entry is
-- created, in the pre-queue before release or at the back of the FIFO after.

local user_id = ARGV[1]
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
-- Never let a safety TTL land in the past (that would delete the key at once).
local expire_at = math.max(tonumber(ARGV[2]), now + 86400000)

local swept = ''
local admission_id = redis.call('HGET', KEYS[2], user_id)
if admission_id then
  local adm_key = ARGV[3] .. admission_id
  local expires = tonumber(redis.call('HGET', adm_key, 'admission_expires_at'))
  if expires and expires > now then
    return { 'admitted', admission_id, '' }
  end
  -- Ended but not swept yet: clean it up so the buyer can rejoin at the back.
  redis.call('DEL', adm_key)
  redis.call('HDEL', KEYS[2], user_id)
  redis.call('ZREM', KEYS[7], admission_id)
  swept = admission_id
end

local status = redis.call('HGET', KEYS[1], 'status')
if status == 'CLOSED' or status == 'CANCELLED' then
  return { 'closed', status, swept }
end

if redis.call('ZSCORE', KEYS[4], user_id) or redis.call('SISMEMBER', KEYS[3], user_id) == 1 then
  return { 'waiting', '', swept }
end

if redis.call('EXISTS', KEYS[6]) == 1 then
  local position = redis.call('INCR', KEYS[5])
  redis.call('ZADD', KEYS[4], position, user_id)
  redis.call('PEXPIREAT', KEYS[4], expire_at)
  redis.call('PEXPIREAT', KEYS[5], expire_at)
else
  redis.call('SADD', KEYS[3], user_id)
  redis.call('PEXPIREAT', KEYS[3], expire_at)
end
return { 'created', '', swept }
