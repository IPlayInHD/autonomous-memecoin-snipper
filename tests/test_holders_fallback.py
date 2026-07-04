"""Holder-data resilience: DAS fallback when getTokenLargestAccounts rejects
Token-2022 mints (live-firehose regression: 'Invalid param: not a Token mint')."""

from conftest import pk
from sniper.chain.provider import RpcChainDataProvider, aggregate_token_accounts
from sniper.chain.rpc import RpcError


class TestAggregation:
    def test_sums_per_owner_and_sorts(self):
        items = [
            {"address": pk(1), "owner": pk(10), "amount": 100},
            {"address": pk(2), "owner": pk(11), "amount": 400},
            {"address": pk(3), "owner": pk(10), "amount": 250},  # same owner as #1
        ]
        holders = aggregate_token_accounts(items)
        assert [h.owner for h in holders] == [pk(11), pk(10)]
        assert holders[1].amount == 350                      # summed across accounts

    def test_ignores_garbage_rows(self):
        items = [
            {"address": pk(1), "owner": None, "amount": 100},
            {"address": pk(2), "owner": pk(11), "amount": 0},
            {"address": pk(3), "owner": pk(12), "amount": "not-a-number"},
            {"address": pk(4), "owner": pk(13), "amount": 5},
        ]
        holders = aggregate_token_accounts(items)
        assert len(holders) == 1 and holders[0].owner == pk(13)

    def test_top_n_cap(self):
        items = [{"address": pk(i), "owner": pk(100 + i), "amount": i + 1}
                 for i in range(50)]
        assert len(aggregate_token_accounts(items, top_n=20)) == 20


class StubRpc:
    """Standard call rejects the mint (as Helius does for T22 pump mints);
    the DAS method serves the data."""

    def __init__(self, das_items, das_fails=False):
        self.das_items = das_items
        self.das_fails = das_fails
        self.das_called = False

    async def get_token_largest_accounts(self, mint):
        raise RpcError("getTokenLargestAccounts", -32602,
                       "Invalid param: not a Token mint")

    async def get_token_accounts_das(self, mint, limit=1000):
        self.das_called = True
        if self.das_fails:
            raise RpcError("getTokenAccounts", -32602, "unsupported")
        return self.das_items


async def test_fallback_to_das_on_rpc_error():
    rpc = StubRpc([{"address": pk(1), "owner": pk(10), "amount": 500},
                   {"address": pk(2), "owner": pk(11), "amount": 700}])
    provider = RpcChainDataProvider(rpc)
    holders = await provider.get_largest_holders(pk(99))
    assert rpc.das_called
    assert [h.owner for h in holders] == [pk(11), pk(10)]


async def test_both_paths_failing_returns_empty_not_crash():
    """Filter must see [] and fail closed with 'holder list unavailable',
    never a raised exception."""
    provider = RpcChainDataProvider(StubRpc([], das_fails=True))
    assert await provider.get_largest_holders(pk(99)) == []
