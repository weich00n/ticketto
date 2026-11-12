-- Sweep expired admissions, then fill free pool slots from the head of the queue.
-- Atomic, so concurrent callers (admitter tick, OrderConfirmed consumer) can
-- never overfill the pool or admit the same buyer twice.
--
-- KEYS[1] config     queue:{eid}:config     (HASH, read: status)
-- KEYS[2] active     queue:{eid}:active     (ZSET admission_id -> expires ms)
-- KEYS[3] user_adm   queue:{eid}:user_adm   (HASH user_id -> admission_id)
-- KEYS[4] waiting    queue:{eid}:waiting    (ZSET user_id -> seq)
-- KEYS[5] shuffled   queue:{eid}:shuffled   (STRING flag)
-- ARGV[1] admission hash key prefix  queue:{eid}:adm:
-- ARGV[2] event_id
-- ARGV[3] pool capacity
-- ARGV[4] selection window (ms)
-- ARGV[5] admission window (ms)
-- ARGV[6] safety expiry (ms epoch)
-- ARGV[7..] pre-generated admission_ids (Lua has no UUIDs); at least as many
--           as the capacity, so every free slot can be filled
--
-- Reply: { released, admitted, now }
--   released  flat { admission_id, user_id, ... }  (user_id '' if hash was gone)
--   admitted  flat { admission_id, user_id, admitted_at, selection_expires_at,
--                    admission_expires_at, ... }  (times in ms)
--
-- A CLOSED/CANCELLED queue, or one not yet shuffled (before release), is only
-- swept: no new admissions.

local prefix, event_id = ARGV[1], ARGV[2]
local capacity = tonumber(ARGV[3])
local selection_ms, admission_ms = tonumber(ARGV[4]), tonumber(ARGV[5])

local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local expire_at = math.max(tonumber(ARGV[6]), now + 86400000)

-- 1. Sweep: every admission whose deadline has passed frees its slot.
local released = {}
for _, admission_id in ipairs(redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', now)) do
  local adm_key = prefix .. admission_id
  local user_id = redis.call('HGET', adm_key, 'user_id')
  if user_id and redis.call('HGET', KEYS[3], user_id) == admission_id then
    redis.call('HDEL', KEYS[3], user_id)
  end
  redis.call('DEL', adm_key)
  redis.call('ZREM', KEYS[2], admission_id)
  released[#released + 1] = admission_id
  released[#released + 1] = user_id or ''
end

-- 2. Only admit once the queue is ADMITTING (shuffled) and not terminal.
local status = redis.call('HGET', KEYS[1], 'status')
if status == 'CLOSED' or status == 'CANCELLED' or redis.call('EXISTS', KEYS[5]) == 0 then
  return { released, {}, now }
end

local free = math.min(capacity - redis.call('ZCARD', KEYS[2]), #ARGV - 6)
local admitted = {}
if free > 0 then
  local popped = redis.call('ZPOPMIN', KEYS[4], free)
  local next_id = 7
  for i = 1, #popped, 2 do
    local user_id = popped[i]
    -- Invariant: a waiting buyer holds no admission (join refuses that). Never
    -- hand out a second one if it is somehow broken; the slot refills next call.
    if redis.call('HEXISTS', KEYS[3], user_id) == 0 then
      local admission_id = ARGV[next_id]
      next_id = next_id + 1
      local selection_expires = now + selection_ms
      local admission_expires = now + admission_ms
      local adm_key = prefix .. admission_id
      redis.call('HSET', adm_key,
        'admission_id', admission_id,
        'user_id', user_id,
        'event_id', event_id,
        'admitted_at', now,
        'selection_expires_at', selection_expires,
        'admission_expires_at', admission_expires)
      redis.call('PEXPIREAT', adm_key, expire_at)
      redis.call('HSET', KEYS[3], user_id, admission_id)
      redis.call('ZADD', KEYS[2], admission_expires, admission_id)
      admitted[#admitted + 1] = admission_id
      admitted[#admitted + 1] = user_id
      admitted[#admitted + 1] = now
      admitted[#admitted + 1] = selection_expires
      admitted[#admitted + 1] = admission_expires
    end
  end
  if #admitted > 0 then
    redis.call('PEXPIREAT', KEYS[2], expire_at)
    redis.call('PEXPIREAT', KEYS[3], expire_at)
  end
end

return { released, admitted, now }
