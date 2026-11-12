-- End an admission because its order was confirmed (OrderConfirmed).
--
-- KEYS[1] active     queue:{eid}:active            (ZSET admission_id -> expires ms)
-- KEYS[2] user_adm   queue:{eid}:user_adm          (HASH user_id -> admission_id)
-- KEYS[3] admission  queue:{eid}:adm:{admission_id} (HASH)
-- ARGV[1] admission_id
-- ARGV[2] user_id   from the message
-- ARGV[3] event_id  from the message
--
-- Reply: { outcome, stored_user_id, stored_event_id }
--   'not_found'  not in the pool (already released or swept): ignore
--   'expired'    deadline passed but not swept yet: change nothing, the sweep
--                will release it (an order for an ended admission is a no-op)
--   'mismatch'   message user_id/event_id differ from the stored admission:
--                change nothing (integrity error, logged by the caller)
--   'released'   admission removed; caller should admit the next buyer

local admission_id = ARGV[1]
local expires = redis.call('ZSCORE', KEYS[1], admission_id)
if not expires then
  return { 'not_found', '', '' }
end

local stored = redis.call('HMGET', KEYS[3], 'user_id', 'event_id')
local stored_user, stored_event = stored[1], stored[2]
if not stored_user then
  -- Pool entry without its hash (should not happen): drop the dangling member.
  redis.call('ZREM', KEYS[1], admission_id)
  return { 'not_found', '', '' }
end

local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
if tonumber(expires) <= now then
  return { 'expired', stored_user, stored_event }
end

if stored_user ~= ARGV[2] or stored_event ~= ARGV[3] then
  return { 'mismatch', stored_user, stored_event }
end

redis.call('DEL', KEYS[3])
if redis.call('HGET', KEYS[2], stored_user) == admission_id then
  redis.call('HDEL', KEYS[2], stored_user)
end
redis.call('ZREM', KEYS[1], admission_id)
return { 'released', stored_user, stored_event }
