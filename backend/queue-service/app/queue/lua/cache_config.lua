-- Write the cached queue config, never letting a stale writer win.
--
-- KEYS[1]  queue:{eid}:config
-- ARGV[1]  PEXPIREAT time (ms)
-- ARGV[2..] field/value pairs; must include status and event_version
--
-- Refuses the write (returns 0) when:
--   * the cached status is terminal and the new one is not (terminal is sticky),
--   * the cached status is CANCELLED and the new one is not CANCELLED
--     (CLOSED may still become CANCELLED, never the reverse),
--   * the cached event_version is newer than the new one.
-- Otherwise writes all fields, sets the expiry and returns 1.

local TERMINAL = { CLOSED = true, CANCELLED = true }

local new = {}
for i = 2, #ARGV, 2 do
  new[ARGV[i]] = ARGV[i + 1]
end

local cur = redis.call('HMGET', KEYS[1], 'status', 'event_version')
local cur_status, cur_version = cur[1], cur[2]

if cur_status then
  if TERMINAL[cur_status] and not TERMINAL[new.status] then
    return 0
  end
  if cur_status == 'CANCELLED' and new.status ~= 'CANCELLED' then
    return 0
  end
  if cur_version and tonumber(cur_version) > tonumber(new.event_version) then
    return 0
  end
end

redis.call('HSET', KEYS[1], unpack(ARGV, 2))
redis.call('PEXPIREAT', KEYS[1], ARGV[1])
return 1
