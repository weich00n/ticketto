-- Takes a hold, or refuses it. One round trip, one atomic step.
--
-- This script is the reason the design exists: the check and the decrement happen together
-- inside Redis, so two buyers racing for the last ticket cannot both be told yes. Nothing may
-- read availability in Java and write it back; that is the exact race this prevents.
--
-- KEYS[1] sale:{eventId}                 ON_SALE or CANCELLED
-- KEYS[2] avail:{eventId}:categoryId     tickets free right now
-- KEYS[3] userqty:{eventId}:userId       held plus bought, for the five-ticket limit
-- KEYS[4] hold:{eventId}:holdId          the hold's own key; its existence means it is alive
-- KEYS[5] zexp:{eventId}:categoryId      expiry index, score = expiry epoch
-- KEYS[6] holdinfo:{eventId}:holdId      how the hold ended; outlives the hold's own key
--
-- ARGV[1] quantity
-- ARGV[2] maxTicketsPerUser
-- ARGV[3] holdTtlSeconds
-- ARGV[4] holdExpiresAtEpochSeconds
-- ARGV[5] holdId
-- ARGV[6] holdInfoTtlSeconds
-- ARGV[7..] field, value, field, value ... written onto the hold hash and the hold info
--
-- Returns { 'OK', availableAfter } (for logs and tests; a sell-out is decided at confirmation, since
-- a held ticket can still come back), or a refusal and the number the caller needs to explain it:
--   { 'EVENT_CANCELLED' }
--   { 'NOT_INITIALISED' }                       counters are missing; this is a Redis problem
--   { 'INSUFFICIENT_INVENTORY', available }
--   { 'PURCHASE_LIMIT_EXCEEDED', remaining }    how many more this buyer may still take

local quantity = tonumber(ARGV[1])
local maxTicketsPerUser = tonumber(ARGV[2])
local holdTtlSeconds = tonumber(ARGV[3])
local holdExpiresAt = tonumber(ARGV[4])
local holdId = ARGV[5]
local holdInfoTtlSeconds = tonumber(ARGV[6])

local saleStatus = redis.call('GET', KEYS[1])
if not saleStatus then
  return { 'NOT_INITIALISED' }
end
if saleStatus ~= 'ON_SALE' then
  return { 'EVENT_CANCELLED' }
end

local available = redis.call('GET', KEYS[2])
if not available then
  return { 'NOT_INITIALISED' }
end
available = tonumber(available)

-- The limit is checked before inventory, so a buyer who is already at five is told the real
-- reason rather than being told the category is full.
local alreadyTaken = tonumber(redis.call('GET', KEYS[3]) or '0')
if alreadyTaken + quantity > maxTicketsPerUser then
  return { 'PURCHASE_LIMIT_EXCEEDED', tostring(math.max(0, maxTicketsPerUser - alreadyTaken)) }
end

if available < quantity then
  return { 'INSUFFICIENT_INVENTORY', tostring(available) }
end

redis.call('DECRBY', KEYS[2], quantity)
redis.call('INCRBY', KEYS[3], quantity)
for i = 7, #ARGV, 2 do
  redis.call('HSET', KEYS[4], ARGV[i], ARGV[i + 1])
  redis.call('HSET', KEYS[6], ARGV[i], ARGV[i + 1])
end
-- The TTL is the liveness rule: once this key dies the hold can no longer be confirmed.
redis.call('EXPIRE', KEYS[4], holdTtlSeconds)
-- The info lives on after the hold, so whoever finds the hold half-finished later can still
-- tell how many tickets it had and whether they went back.
redis.call('HSET', KEYS[6], 'state', 'HELD')
redis.call('EXPIRE', KEYS[6], holdInfoTtlSeconds)
redis.call('ZADD', KEYS[5], holdExpiresAt, holdId)

return { 'OK', tostring(available - quantity) }
