-- Turn the unordered pre-queue into FIFO order, exactly once per event.
--
-- Python reads the pre-queue, shuffles it with secrets.SystemRandom and passes
-- the order in; Lua randomness is not suitable, so no shuffling happens here.
--
-- KEYS[1] shuffled   queue:{eid}:shuffled   (STRING flag)
-- KEYS[2] prequeue   queue:{eid}:prequeue   (SET)
-- KEYS[3] waiting    queue:{eid}:waiting    (ZSET user_id -> seq)
-- KEYS[4] seq        queue:{eid}:seq        (STRING counter)
-- ARGV[1]   safety expiry (ms epoch)
-- ARGV[2..] user_ids in shuffled order
--
-- Reply: { performed (1|0), ordered, leftovers }
--   performed 0: the flag was already set, nothing changed.
--   ordered:    ARGV members still in the pre-queue, placed in the given order.
--   leftovers:  members that joined after Python read the pre-queue; appended
--               after the shuffled ones, in SMEMBERS order (arbitrary, but they
--               all joined during the same pre-release window).

local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local expire_at = math.max(tonumber(ARGV[1]), now + 86400000)

if not redis.call('SET', KEYS[1], '1', 'NX') then
  return { 0, 0, 0 }
end

local ordered = 0
for i = 2, #ARGV do
  -- SREM doubles as the membership check and guards against duplicates in ARGV.
  if redis.call('SREM', KEYS[2], ARGV[i]) == 1 then
    redis.call('ZADD', KEYS[3], redis.call('INCR', KEYS[4]), ARGV[i])
    ordered = ordered + 1
  end
end

local rest = redis.call('SMEMBERS', KEYS[2])
for _, user_id in ipairs(rest) do
  redis.call('ZADD', KEYS[3], redis.call('INCR', KEYS[4]), user_id)
end
redis.call('DEL', KEYS[2])

redis.call('PEXPIREAT', KEYS[1], expire_at)
if redis.call('EXISTS', KEYS[3]) == 1 then
  redis.call('PEXPIREAT', KEYS[3], expire_at)
  redis.call('PEXPIREAT', KEYS[4], expire_at)
end
return { 1, ordered, #rest }
