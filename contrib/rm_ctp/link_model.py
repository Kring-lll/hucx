#!/usr/bin/env python3
"""Executable A4 design model; no URMA, MPI, packet or performance evidence.

The model checks ownership and credit invariants before changing the transport.
Run with Python 3.8+; standard library only. A source is a remote RM iface, and
all its logical EPs share one transmit credit account for a destination iface.
"""

from dataclasses import dataclass, field, replace
import random
import unittest


@dataclass(frozen=True)
class TargetKey:
    eid: str
    uasid: int
    jetty: int
    segment: tuple = (4096, 64, 1, 7)
    mode: str = 'RM+CTP'
    version: int = 1
    # Effective import settings belong to the key or must be iface constants.
    order: str = 'default'
    token: int = 0
    instance: int = 1


class TargetCache:
    def __init__(self):
        self.entries = {}
        self.imports = 0
        self.rollbacks = 0
        self.releases = 0

    def get(self, key, fail_segment=False):
        if key in self.entries:
            return self.entries[key]
        self.imports += 1
        candidate = object()
        if fail_segment:
            self.rollbacks += 1
            raise RuntimeError('segment import failed; target rolled back')
        self.entries[key] = candidate
        return candidate

    def close(self, quiesced):
        if not quiesced:
            raise RuntimeError('shared Jetty must quiesce before target release')
        self.releases += len(self.entries)
        self.entries.clear()


@dataclass
class PeerTx:
    window: int
    credits: int
    eps: set = field(default_factory=set)
    request_id: int = 0
    sent: int = field(init=False)
    requested: int = 0

    def __post_init__(self):
        self.sent = self.window - self.credits

    def send(self, ep):
        if ep not in self.eps or self.credits == 0:
            return False
        self.credits -= 1
        self.sent += 1
        return True


class Requests:
    def __init__(self, instance=1):
        self.instance = instance
        self.sequence = 0
        self.active = {}

    def begin(self, peer):
        if peer.credits > peer.window // 4 or peer.request_id:
            raise RuntimeError('one outstanding request at the hard threshold')
        count = peer.sent - peer.requested
        if not 0 < count <= peer.window:
            raise RuntimeError('request only newly spent credits')
        if self.sequence == (1 << 64) - 1:
            raise OverflowError('never reuse a request identifier')
        self.sequence += 1
        peer.request_id = self.sequence
        peer.requested = peer.sent
        self.active[self.sequence] = (peer, count)
        return self.sequence

    def grant(self, identifier, credits, instance=None):
        if instance is not None and instance != self.instance:
            return False
        entry = self.active.get(identifier)
        if entry is None:
            return False
        peer, count = entry
        if credits != count or peer.credits + credits > peer.window:
            raise RuntimeError('invalid grant amount or request state')
        del self.active[identifier]
        peer.request_id = 0
        peer.credits += credits
        return True

    def cancel(self, peer):
        entry = self.active.pop(peer.request_id, None)
        if entry is not None:
            peer.requested -= entry[1]
        peer.request_id = 0


class Receiver:
    """Aggregate budget for a configured, bounded set of remote ifaces.

    'issued' includes unused advertised credits, in-flight data, and received
    data whose RX positions have not been reposted. Only actual successful RX
    reposting retires that last obligation. Receipt alone creates no credit.
    """

    def __init__(self, depth, max_sources, window):
        if min(depth, max_sources, window) <= 0:
            raise ValueError('positive depth, source bound and window required')
        self.control_capacity = 2 * max_sources + 2
        self.data_capacity = depth - self.control_capacity
        self.issued = max_sources * window
        if self.issued > self.data_capacity:
            raise ValueError('initial data credits plus control reserve exceed RX')
        self.depth = depth
        self.posted = depth
        self.free = self.data_capacity - self.issued
        self.data_debt = 0
        self.control_debt = 0
        self.released_data = 0
        self.check()

    def receive(self, control=False, held=False):
        if self.posted == 0:
            raise RuntimeError('RX exhausted')
        self.posted -= 1
        if control:
            self.control_debt += 1
        else:
            self.data_debt += 1
            if not held:
                self.released_data += 1
        self.check()

    def release(self, count):
        if not 0 <= count <= self.data_debt - self.released_data:
            raise ValueError('cannot release an unreceived/already released buffer')
        self.released_data += count

    def repost(self, count):
        if not 0 <= count <= self.control_debt + self.released_data:
            raise ValueError('repost requires available RX descriptors')
        # Account successful reposts conservatively against control debt first.
        control = min(count, self.control_debt)
        data = count - control
        self.control_debt -= control
        self.data_debt -= data
        self.released_data -= data
        self.issued -= data
        self.free += data
        self.posted += count
        self.check()

    def grant(self, count):
        # No new control emission until consumed control positions are restored.
        if self.control_debt or not 0 < count <= self.free:
            return False
        self.free -= count
        self.issued += count
        self.check()
        return True

    def check(self):
        assert 0 <= self.posted <= self.depth
        assert 0 <= self.free <= self.data_capacity
        assert 0 <= self.issued <= self.data_capacity
        assert self.free + self.issued == self.data_capacity
        assert self.depth - self.posted == self.data_debt + self.control_debt
        assert 0 <= self.released_data <= self.data_debt
        assert 0 <= self.control_debt <= self.control_capacity


class ReplyRouter:
    """One iface control owner, sharing the local Jetty; no reverse data EP.

    Inline posts copy the reply immediately. Non-inline posts keep a distinct
    buffer until their CQE. Queued/in-flight replies retain target ownership.
    This models the existing UCX pending/mpool/CQE rules, not a new provider.
    """

    def __init__(self, cache, depth=2):
        self.cache = cache
        self.depth = depth
        self.pending = []
        self.inflight = {}
        self.submitted = 0
        self.data_eps = set()
        self.last_request = {}

    def enqueue(self, key, request_id):
        if request_id <= self.last_request.get(key, 0):
            return False
        target = self.cache.get(key)
        self.pending.append((target, request_id))
        self.last_request[key] = request_id
        return True

    def progress(self, transient=False, inline=False):
        if transient or not self.pending or len(self.inflight) == self.depth:
            return False
        target, request_id = self.pending.pop(0)
        self.submitted += 1
        buffer = None if inline else bytearray(request_id.to_bytes(8, 'little'))
        self.inflight[self.submitted] = (target, request_id, buffer)
        return True

    def complete(self, sequence):
        del self.inflight[sequence]

    def close(self, quiesced=False):
        if self.inflight and not quiesced:
            raise RuntimeError('control DMA still in flight')
        self.pending.clear()
        self.inflight.clear()
        self.cache.close(quiesced=True)


class LinkModelTests(unittest.TestCase):
    def test_complete_target_key_and_atomic_import(self):
        cache = TargetCache()
        key = TargetKey('eid-a', 1, 7)
        self.assertIs(cache.get(key), cache.get(key))
        for other in (replace(key, uasid=2), replace(key, jetty=8),
                      replace(key, eid='eid-b'), replace(key, token=9),
                      replace(key, instance=2),
                      replace(key, segment=(8192, 64, 1, 7))):
            self.assertIsNot(cache.get(key), cache.get(other))
        failed = replace(key, jetty=99)
        with self.assertRaises(RuntimeError):
            cache.get(failed, fail_segment=True)
        self.assertNotIn(failed, cache.entries)
        self.assertEqual(cache.rollbacks, 1)
        with self.assertRaises(RuntimeError):
            cache.close(quiesced=False)
        count = len(cache.entries)
        cache.close(quiesced=True)
        self.assertEqual(cache.releases, count)

    def test_reply_without_reverse_ep_and_non_inline_lifetime(self):
        cache = TargetCache()
        router = ReplyRouter(cache)
        key = TargetKey('eid-a', 1, 7)
        router.enqueue(key, 100)
        router.enqueue(key, 101)
        self.assertFalse(router.enqueue(key, 100))
        self.assertFalse(router.enqueue(key, 101))
        self.assertEqual(router.data_eps, set())
        self.assertFalse(router.progress(transient=True))
        self.assertEqual(len(router.pending), 2)
        self.assertTrue(router.progress())
        self.assertTrue(router.progress())
        first = router.inflight[1][2]
        second = router.inflight[2][2]
        self.assertIsNot(first, second)
        self.assertEqual(int.from_bytes(first, 'little'), 100)
        router.enqueue(key, 102)
        self.assertFalse(router.progress())
        with self.assertRaises(RuntimeError):
            router.close()
        router.complete(1)
        self.assertTrue(router.progress(inline=True))
        self.assertEqual(cache.imports, 1)
        router.complete(2)
        router.complete(3)
        router.close(quiesced=True)
        self.assertEqual(cache.releases, 1)

    def test_duplicate_late_grant_and_ep_destruction(self):
        requests = Requests()
        peer = PeerTx(8, 0, {1, 2})
        first = requests.begin(peer)
        peer.eps.remove(1)
        # Identity belongs to the peer credit account, not a destroyed EP.
        self.assertTrue(requests.grant(first, 8))
        self.assertFalse(requests.grant(first, 8))
        peer.eps.add(3)
        self.assertEqual(peer.credits, 8)
        self.assertTrue(peer.send(3))
        self.assertEqual(peer.credits, 7)
        while peer.send(3):
            pass
        old = requests.begin(peer)
        requests.cancel(peer)
        current = requests.begin(peer)
        self.assertNotEqual(old, current)
        self.assertFalse(requests.grant(old, 8))
        self.assertEqual(peer.credits, 0)
        self.assertTrue(requests.grant(current, 8))
        requests.sequence = (1 << 64) - 1
        while peer.send(3):
            pass
        with self.assertRaises(OverflowError):
            requests.begin(peer)

        restarted = Requests(instance=2)
        fresh = PeerTx(8, 0, {1})
        reused_number = restarted.begin(fresh)
        self.assertEqual(reused_number, first)
        self.assertFalse(restarted.grant(first, 8, instance=1))
        self.assertTrue(restarted.grant(reused_number, 8, instance=2))

    def test_logical_eps_share_one_credit_account(self):
        peer = PeerTx(8, 8, {1, 2, 3})
        for i in range(8):
            self.assertTrue(peer.send(1 + i % 3))
        for ep in peer.eps:
            self.assertFalse(peer.send(ep))
        peer.eps.add(4)
        self.assertFalse(peer.send(4))

    def test_early_request_adds_spent_credits_without_window_reset(self):
        peer = PeerTx(8, 8, {1})
        requests = Requests()
        for _ in range(6):
            self.assertTrue(peer.send(1))
        identifier = requests.begin(peer)
        self.assertTrue(peer.send(1))
        with self.assertRaises(RuntimeError):
            requests.grant(identifier, 8)
        self.assertTrue(requests.grant(identifier, 6))
        self.assertEqual(peer.credits, 7)
        self.assertFalse(requests.grant(identifier, 6))

    def test_budget_rejects_unsafe_scale(self):
        Receiver(64, 4, 8)
        with self.assertRaises(ValueError):
            Receiver(64, 20, 8)
        # Counts assume one selected RM iface per rank; actual nodes untested.
        Receiver(512, 39, 8)
        Receiver(4096, 383, 8)

    def test_receipt_and_held_buffer_do_not_restore_credit(self):
        receiver = Receiver(14, 2, 4)
        self.assertEqual(receiver.free, 0)
        receiver.receive(held=True)
        self.assertFalse(receiver.grant(1))
        with self.assertRaises(ValueError):
            receiver.repost(1)
        receiver.release(1)
        self.assertFalse(receiver.grant(1))
        receiver.repost(1)
        self.assertTrue(receiver.grant(1))

    def test_control_repost_precedes_new_control_emission(self):
        receiver = Receiver(64, 4, 8)
        receiver.receive(control=True)
        self.assertFalse(receiver.grant(8))
        receiver.repost(1)
        self.assertTrue(receiver.grant(8))

    def test_bidirectional_early_requests_before_data(self):
        receivers = [Receiver(8, 1, 4), Receiver(8, 1, 4)]
        peers = [PeerTx(4, 4, {1}), PeerTx(4, 4, {1})]
        requests = [Requests(instance=1), Requests(instance=2)]
        identifiers = []
        for peer, registry in zip(peers, requests):
            for _ in range(3):
                self.assertTrue(peer.send(1))
            identifiers.append(registry.begin(peer))
        for receiver in receivers:
            # Requests arrive before their data; this cannot manufacture credit.
            receiver.receive(control=True)
            self.assertFalse(receiver.grant(3))
            receiver.repost(1)
            self.assertFalse(receiver.grant(3))
            for _ in range(3):
                receiver.receive()
            receiver.repost(3)
            self.assertTrue(receiver.grant(3))
        for index, receiver in enumerate(receivers):
            receiver.receive(control=True)
            self.assertTrue(requests[index].grant(identifiers[index], 3))
            self.assertEqual(peers[index].credits, 4)
            receiver.repost(1)
            self.assertEqual(receiver.posted, receiver.depth)

    def test_reordered_data_requests_and_grants(self):
        for seed in range(12):
            rng = random.Random(seed)
            receiver = Receiver(64, 4, 8)
            peers = [PeerTx(8, 8, {0, 1}) for _ in range(4)]
            requests = Requests()
            data = []
            control = []
            waiting = []
            grants = []
            sent = received = 0

            def check():
                receiver.check()
                obligations = sum(p.credits for p in peers) + len(data)
                obligations += receiver.data_debt + sum(n for _, n in grants)
                self.assertEqual(receiver.issued, obligations)
                self.assertLessEqual(len(requests.active), len(peers))

            for _ in range(5000):
                event = rng.randrange(7)
                peer = rng.choice(peers)
                if event == 0 and peer.send(rng.randrange(2)):
                    data.append(peer)
                    sent += 1
                elif event == 1 and data:
                    data.pop(rng.randrange(len(data)))
                    receiver.receive(held=rng.randrange(4) == 0)
                    received += 1
                elif (event == 2 and peer.credits <= peer.window // 4 and
                      not peer.request_id):
                    identifier = requests.begin(peer)
                    control.append((identifier, requests.active[identifier][1]))
                elif event == 3 and control:
                    entry = control.pop(rng.randrange(len(control)))
                    receiver.receive(control=True)
                    waiting.append(entry)
                elif event == 4:
                    held = receiver.data_debt - receiver.released_data
                    receiver.release(rng.randrange(held + 1))
                    available = receiver.control_debt + receiver.released_data
                    receiver.repost(rng.randrange(available + 1))
                elif event == 5 and waiting and receiver.grant(waiting[0][1]):
                    grants.append(waiting.pop(0))
                elif event == 6 and grants:
                    identifier, credits = grants.pop(rng.randrange(len(grants)))
                    self.assertTrue(requests.grant(identifier, credits))
                    self.assertFalse(requests.grant(identifier, credits))
                check()

            # Stop creating traffic; prove the pending controls can drain.
            for _ in range(100):
                if data:
                    data.pop()
                    receiver.receive()
                    received += 1
                if control:
                    receiver.receive(control=True)
                    waiting.append(control.pop())
                receiver.release(receiver.data_debt - receiver.released_data)
                receiver.repost(receiver.control_debt + receiver.released_data)
                if waiting and receiver.grant(waiting[0][1]):
                    grants.append(waiting.pop(0))
                if grants:
                    identifier, credits = grants.pop()
                    self.assertTrue(requests.grant(identifier, credits))
                check()
                if not data and not control and not waiting and not grants:
                    break
            self.assertFalse(data or control or waiting or grants)
            self.assertEqual(sent, received)
            self.assertEqual(receiver.posted, receiver.depth)


if __name__ == '__main__':
    unittest.main(verbosity=2)
