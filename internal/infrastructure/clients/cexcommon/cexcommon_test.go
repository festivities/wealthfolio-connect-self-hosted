package cexcommon_test

import (
	"testing"
	"time"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"

	"github.com/wealthfolio/wealthfolio-connect-self-hosted/internal/infrastructure/clients/cexcommon"
)

func TestCEXCommon(t *testing.T) {
	RegisterFailHandler(Fail)
	RunSpecs(t, "cexcommon Suite")
}

var _ = Describe("Translate", func() {
	It("collapses stablecoins into the cash balance", func() {
		snap := cexcommon.Translate("test", "Test", cexcommon.Snapshot{
			Balances: []cexcommon.Balance{
				{Asset: "USDT", Quantity: 100, PriceUSD: 1, USDValue: 100},
				{Asset: "USDC", Quantity: 50, PriceUSD: 1, USDValue: 50},
			},
		})
		Expect(snap.Holdings).To(HaveLen(1))
		Expect(snap.Holdings[0].Balances[0].Cash).To(Equal(150.0))
		Expect(snap.Holdings[0].Positions).To(BeEmpty())
		Expect(snap.Accounts[0].BalanceTotal).To(Equal(150.0))
	})

	It("creates a position per non-stable asset and skips dust under $1", func() {
		snap := cexcommon.Translate("test", "Test", cexcommon.Snapshot{
			Balances: []cexcommon.Balance{
				{Asset: "BTC", Quantity: 0.5, PriceUSD: 60000, USDValue: 30000},
				{Asset: "DUST", Quantity: 1000, PriceUSD: 0.0001, USDValue: 0.1},
			},
		})
		Expect(snap.Holdings[0].Positions).To(HaveLen(1))
		Expect(snap.Holdings[0].Positions[0].Symbol.Symbol).To(Equal("BTC"))
		Expect(snap.Holdings[0].Positions[0].Units).To(Equal(0.5))
		Expect(snap.Holdings[0].Positions[0].AveragePurchasePrice).To(BeZero())
	})

	It("translates trades into BUY/SELL activities", func() {
		snap := cexcommon.Translate("test", "Test", cexcommon.Snapshot{
			Trades: []cexcommon.Trade{
				{ID: "t1", Symbol: "BTC-USDT", Side: "buy", Price: 60000, Quantity: 0.1, Timestamp: time.Now()},
				{ID: "t2", Symbol: "BTC-USDT", Side: "SELL", Price: 65000, Quantity: 0.05, Timestamp: time.Now()},
			},
		})
		acts := snap.Activities["test-spot"]
		Expect(acts).To(HaveLen(2))
		Expect(string(acts[0].Type)).To(Equal("BUY"))
		Expect(string(acts[1].Type)).To(Equal("SELL"))
		Expect(acts[0].Amount).To(Equal(6000.0))
		Expect(acts[0].Currency.Code).To(Equal("USD"))
		Expect(acts[0].Symbol.Currency.Code).To(Equal("USD"))
	})

	It("derives the average cost from USD trades, including sells", func() {
		snap := cexcommon.Translate("binance", "Binance", cexcommon.Snapshot{
			Balances: []cexcommon.Balance{
				{Asset: "BTC", Quantity: 0.75, PriceUSD: 60000, USDValue: 45000},
			},
			Trades: []cexcommon.Trade{
				{ID: "t1", Symbol: "BTCUSDT", Side: "buy", Price: 60000, Quantity: 0.5, Currency: "USD", Timestamp: time.Unix(1000, 0)},
				{ID: "t3", Symbol: "BTCUSDT", Side: "buy", Price: 62000, Quantity: 0.5, Currency: "USD", Timestamp: time.Unix(2000, 0)},
				{ID: "t2", Symbol: "BTCUSDT", Side: "sell", Price: 65000, Quantity: 0.25, Currency: "USD", Timestamp: time.Unix(3000, 0)},
			},
		})
		Expect(snap.Holdings[0].Positions).To(HaveLen(1))
		Expect(snap.Holdings[0].Positions[0].AveragePurchasePrice).To(Equal(61000.0))
	})

	It("counts a USD-denominated fiat buy toward the average cost", func() {
		snap := cexcommon.Translate("binance", "Binance", cexcommon.Snapshot{
			Balances: []cexcommon.Balance{
				{Asset: "SOL", Quantity: 2.5, PriceUSD: 160, USDValue: 400},
			},
			Trades: []cexcommon.Trade{
				{ID: "fiat:1", Symbol: "SOL", Side: "buy", Price: 160, Quantity: 2.5, Currency: "USD", SymbolCurrency: "USD", Amount: 400, Timestamp: time.Unix(1000, 0)},
			},
		})
		Expect(snap.Holdings[0].Positions[0].AveragePurchasePrice).To(Equal(160.0))
	})

	It("omits the average cost when no trade history explains the position", func() {
		snap := cexcommon.Translate("test", "Test", cexcommon.Snapshot{
			Balances: []cexcommon.Balance{
				{Asset: "ETH", Quantity: 2, PriceUSD: 3000, USDValue: 6000},
			},
		})
		Expect(snap.Holdings[0].Positions[0].AveragePurchasePrice).To(BeZero())
	})

	It("omits the average cost for a position funded in a non-USD fiat currency", func() {
		snap := cexcommon.Translate("binance", "Binance", cexcommon.Snapshot{
			Balances: []cexcommon.Balance{
				{Asset: "SOL", Quantity: 2.5, PriceUSD: 160, USDValue: 400},
			},
			Trades: []cexcommon.Trade{
				{ID: "fiat:1", Symbol: "SOL", Side: "buy", Price: 400, Quantity: 2.5, Currency: "PHP", SymbolCurrency: "USD", Amount: 1000, Timestamp: time.Unix(1000, 0)},
			},
		})
		Expect(snap.Holdings[0].Positions[0].AveragePurchasePrice).To(BeZero())
	})

	It("omits the average cost when trades only partly explain the position", func() {
		snap := cexcommon.Translate("binance", "Binance", cexcommon.Snapshot{
			Balances: []cexcommon.Balance{
				{Asset: "SOL", Quantity: 5, PriceUSD: 160, USDValue: 800},
			},
			Trades: []cexcommon.Trade{
				{ID: "spot:1", Symbol: "SOLUSDT", Side: "buy", Price: 160, Quantity: 2.5, Currency: "USD", Timestamp: time.Unix(1000, 0)},
				{ID: "fiat:1", Symbol: "SOL", Side: "buy", Price: 400, Quantity: 2.5, Currency: "PHP", SymbolCurrency: "USD", Amount: 1000, Timestamp: time.Unix(2000, 0)},
			},
		})
		Expect(snap.Holdings[0].Positions[0].AveragePurchasePrice).To(BeZero())
	})

	It("omits the average cost when the history has an uncovered sell", func() {
		snap := cexcommon.Translate("binance", "Binance", cexcommon.Snapshot{
			Balances: []cexcommon.Balance{
				{Asset: "BTC", Quantity: 1, PriceUSD: 60000, USDValue: 60000},
			},
			Trades: []cexcommon.Trade{
				{ID: "t1", Symbol: "BTCUSDT", Side: "sell", Price: 60000, Quantity: 1, Currency: "USD", Timestamp: time.Unix(1000, 0)},
			},
		})
		Expect(snap.Holdings[0].Positions[0].AveragePurchasePrice).To(BeZero())
	})

	It("derives the average cost from separated pair symbols", func() {
		snap := cexcommon.Translate("okx", "OKX", cexcommon.Snapshot{
			Balances: []cexcommon.Balance{
				{Asset: "BTC", Quantity: 0.75, PriceUSD: 60000, USDValue: 45000},
			},
			Trades: []cexcommon.Trade{
				{ID: "t1", Symbol: "BTC-USDT", Side: "buy", Price: 60000, Quantity: 0.5, Timestamp: time.Unix(1000, 0)},
				{ID: "t3", Symbol: "BTC/USDT", Side: "buy", Price: 62000, Quantity: 0.5, Timestamp: time.Unix(2000, 0)},
				{ID: "t2", Symbol: "BTCUSDT", Side: "sell", Price: 65000, Quantity: 0.25, Timestamp: time.Unix(3000, 0)},
			},
		})
		Expect(snap.Holdings[0].Positions[0].AveragePurchasePrice).To(Equal(61000.0))
	})

	It("does not treat a tiny materially-different balance as fully covered", func() {
		// Held 1e-8 but only half is explained by trades: an absolute 1e-8
		// tolerance would call this covered. The relative tolerance must not.
		snap := cexcommon.Translate("test", "Test", cexcommon.Snapshot{
			Balances: []cexcommon.Balance{
				{Asset: "SHIB", Quantity: 1e-8, PriceUSD: 1e8, USDValue: 1},
			},
			Trades: []cexcommon.Trade{
				{ID: "t1", Symbol: "SHIBUSDT", Side: "buy", Price: 1, Quantity: 5e-9, Currency: "USD", Timestamp: time.Unix(1000, 0)},
			},
		})
		Expect(snap.Holdings[0].Positions).To(HaveLen(1))
		Expect(snap.Holdings[0].Positions[0].AveragePurchasePrice).To(BeZero())
	})

	It("derives the average cost for a tiny fully-explained balance", func() {
		snap := cexcommon.Translate("test", "Test", cexcommon.Snapshot{
			Balances: []cexcommon.Balance{
				{Asset: "SHIB", Quantity: 2e-8, PriceUSD: 1e8, USDValue: 2},
			},
			Trades: []cexcommon.Trade{
				{ID: "t1", Symbol: "SHIBUSDT", Side: "buy", Price: 0.5, Quantity: 2e-8, Currency: "USD", Timestamp: time.Unix(1000, 0)},
			},
		})
		Expect(snap.Holdings[0].Positions).To(HaveLen(1))
		Expect(snap.Holdings[0].Positions[0].AveragePurchasePrice).To(Equal(0.5))
	})

	It("omits the average cost when a non-USD fill touches the asset even if USD trades net the balance", func() {
		// Buy 1 BTC with USD, then buy 1 BTC with PHP and sell 1 BTC: the USD
		// ledger nets the held 1 BTC, but the PHP leg's cost is unknown, so no
		// basis can be claimed.
		snap := cexcommon.Translate("binance", "Binance", cexcommon.Snapshot{
			Balances: []cexcommon.Balance{
				{Asset: "BTC", Quantity: 1, PriceUSD: 60000, USDValue: 60000},
			},
			Trades: []cexcommon.Trade{
				{ID: "spot:1", Symbol: "BTCUSDT", Side: "buy", Price: 60000, Quantity: 1, Currency: "USD", Timestamp: time.Unix(1000, 0)},
				{ID: "fiat:1", Symbol: "BTC", Side: "buy", Price: 3000000, Quantity: 1, Currency: "PHP", SymbolCurrency: "USD", Amount: 3000000, Timestamp: time.Unix(2000, 0)},
				{ID: "spot:2", Symbol: "BTCUSDT", Side: "sell", Price: 60000, Quantity: 1, Currency: "USD", Timestamp: time.Unix(3000, 0)},
			},
		})
		Expect(snap.Holdings[0].Positions[0].AveragePurchasePrice).To(BeZero())
	})

	It("uses explicit trade amount and currency overrides", func() {
		snap := cexcommon.Translate("binance", "Binance", cexcommon.Snapshot{
			Trades: []cexcommon.Trade{{
				ID: "fiat:1", Symbol: "SOL", Side: "buy", Price: 400,
				Quantity: 2.5, Amount: 1000, Currency: "PHP", SymbolCurrency: "USD",
			}},
		})
		activity := snap.Activities["binance-spot"][0]
		Expect(activity.Amount).To(Equal(1000.0))
		Expect(activity.Currency.Code).To(Equal("PHP"))
		Expect(activity.Symbol.Currency.Code).To(Equal("USD"))
	})

	It("marks an empty successful trade query as synced", func() {
		snap := cexcommon.Translate("test", "Test", cexcommon.Snapshot{ActivitiesFetched: true})
		Expect(snap.Activities).To(BeEmpty())
		Expect(snap.Accounts[0].InitialTxSyncDone).To(BeTrue())
		Expect(snap.Accounts[0].LastTxSync).NotTo(BeNil())
	})

	It("uses stable connection IDs derived from the slug", func() {
		snap := cexcommon.Translate("okx", "OKX", cexcommon.Snapshot{})
		Expect(snap.Connection.ID).To(Equal("okx-conn"))
		Expect(snap.Accounts[0].ID).To(Equal("okx-spot"))
	})
})

var _ = Describe("IsStablecoin", func() {
	It("recognizes common USD-pegged coins regardless of case", func() {
		Expect(cexcommon.IsStablecoin("USDT")).To(BeTrue())
		Expect(cexcommon.IsStablecoin("usdc")).To(BeTrue())
		Expect(cexcommon.IsStablecoin("DAI")).To(BeTrue())
		Expect(cexcommon.IsStablecoin("BTC")).To(BeFalse())
	})
})
