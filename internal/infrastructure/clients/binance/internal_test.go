package binance

import (
	"context"
	"testing"
)

// TestRealFetcher_PropagatesNetworkErrors exercises the real Binance SDK
// wrappers without doing any actual network I/O: a pre-canceled context
// makes each call fail before an HTTP request leaves the process.
func TestRealFetcher_PropagatesNetworkErrors(t *testing.T) {
	c := New("k", "s", nil) // nil → realFetcher
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	rf, ok := c.fetcher.(*realFetcher)
	if !ok {
		t.Fatalf("expected *realFetcher, got %T", c.fetcher)
	}

	if _, err := rf.Account(ctx); err == nil {
		t.Error("Account: expected error from canceled context")
	}
	if _, err := rf.Prices(ctx); err == nil {
		t.Error("Prices: expected error from canceled context")
	}
	if _, err := rf.Trades(ctx, "BTCUSDT", 0); err == nil {
		t.Error("Trades: expected error from canceled context")
	}
	if _, err := rf.FiatPayments(ctx, "0", 1, fiatPaymentPageSize); err == nil {
		t.Error("FiatPayments: expected error from canceled context")
	}
}
