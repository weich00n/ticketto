-- A waiting buyer gives up their place (DELETE /queue/me).
--
-- KEYS[1] user_adm   queue:{eid}:user_adm   (HASH user_id -> admission_id)
-- KEYS[2] waiting    queue:{eid}:waiting    (ZSET)
-- KEYS[3] prequeue   queue:{eid}:prequeue   (SET)
-- ARGV[1] user_id
-- ARGV[2] admission hash key prefix  queue:{eid}:adm:
--
-- Reply: 'already_admitted' | 'left' | 'not_in_queue'
-- An admission whose deadline has passed no longer counts as admitted.

local user_id = ARGV[1]
local admission_id = redis.call('HGET', KEYS[1], user_id)
if admission_id then
  local expires = tonumber(redis.call('HGET', ARGV[2] .. admission_id, 'admission_expires_at'))
  local t = redis.call('TIME')
  local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
  if expires and expires > now then
    return 'already_admitted'
  end
end

if redis.call('ZREM', KEYS[2], user_id) == 1 or redis.call('SREM', KEYS[3], user_id) == 1 then
  return 'left'
end
return 'not_in_queue'
