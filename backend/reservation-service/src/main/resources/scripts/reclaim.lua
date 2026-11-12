-- Gives a hold's tickets back, exactly once.
--
-- ZREM is the atomic claim, and it is the rule the whole lifecycle rests on:
--
--   whoever removes the member from the expiry index owns that hold's ending.
--
-- Every path that ends a hold competes for it - the buyer's own DELETE, the expiry worker,
-- a confirmation and an event cancellation - and Redis lets exactly one win. The winner alone changes
-- inventory and writes the hold's final status; every loser is told ALREADY_RECLAIMED and
-- does nothing. That is what makes a double refund impossible without any locking.
--
-- Anything added later that ends a hold must win this claim first. A path that transitions a hold without claiming it would be able to
-- return the same tickets a second time.
--
-- KEYS[1] zexp:{eventId}:categoryId    the claim
-- KEYS[2] avail:{eventId}:categoryId
-- KEYS[3] userqty:{eventId}:userId
-- KEYS[4] hold:{eventId}:holdId
-- KEYS[5] holdinfo:{eventId}:holdId
--
-- ARGV[1] holdId
-- ARGV[2] quantity
--
-- Returns { 'OK' } if this call released it, { 'ALREADY_RECLAIMED' } if someone else did.

local holdId = ARGV[1]
local quantity = tonumber(ARGV[2])

if redis.call('ZREM', KEYS[1], holdId) == 0 then
  return { 'ALREADY_RECLAIMED' }
end

redis.call('INCRBY', KEYS[2], quantity)
redis.call('DECRBY', KEYS[3], quantity)
redis.call('DEL', KEYS[4])
-- Recorded so a reconciliation that finds the MySQL row still ACTIVE knows the tickets are
-- already back. Only an existing hash is updated: HSET on a missing key would create one with
-- no TTL and no quantity.
if redis.call('EXISTS', KEYS[5]) == 1 then
  redis.call('HSET', KEYS[5], 'state', 'RETURNED')
end

return { 'OK' }
