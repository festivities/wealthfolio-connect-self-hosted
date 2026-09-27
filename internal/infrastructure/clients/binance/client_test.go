package binance

import (
	"context"
	"errors"
	"testing"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"

	"github.com/wealthfolio/wealthfolio-connect-self-hosted/internal/domain/brokerage"
)

func TestBinance(t *testing.T) {
	RegisterFailHandler(Fail)
	RunSpecs(t, "Binance Client Suite")
}

type fakeFetcher struct {
	balances   []RawBalance
	prices     map[string]float64
	trades     map[string][]UserTrade
	tradePages map[string]map[int64][]UserTrade
	tradeCalls []tradeCall
	fiatPages  map[string]map[int32]FiatPaymentPage
	fiatCalls  []fiatPaymentCall
	balErr     error
	priceErr   error
	tradeErr   error
	fiatErr    error
}

type tradeCall struct {
	symbol    string
	endTimeMs int64
}

type fiatPaymentCall struct {
	transactionType string
	page            int32
	rows            int32
}

func (f *fakeFetcher) Account(_ context.Context) ([]RawBalance, error) {
	return f.balances, f.balErr
}
func (f *fakeFetcher) Prices(_ context.Context) (map[string]float64, error) {
	return f.prices, f.priceErr
}
func (f *fakeFetcher) Trades(_ context.Context, symbol string, endTimeMs int64) ([]UserTrade, error) {
	f.tradeCalls = append(f.tradeCalls, tradeCall{symbol: symbol, endTimeMs: endTimeMs})
	if f.tradeErr != nil {
		return nil, f.tradeErr
	}
	if pages, ok := f.tradePages[symbol]; ok {
		return pages[endTimeMs], nil
	}
	return f.trades[symbol], nil
}
func (f *fakeFetcher) FiatPayments(_ context.Context, transactionType string, page, rows int32) (FiatPaymentPage, error) {
	f.fiatCalls = append(f.fiatCalls, fiatPaymentCall{transactionType: transactionType, page: page, rows: rows})
	if f.fiatErr != nil {
		return FiatPaymentPage{}, f.fiatErr
	}
	if pages, ok := f.fiatPages[transactionType]; ok {
		return pages[page], nil
	}
	return FiatPaymentPage{Success: true}, nil
}

var _ = Describe("Binance Client", func() {
	It("returns slug binance", func() {
		Expect(New("k", "s", &fakeFetcher{}).ID()).To(Equal("binance"))
	})

	It("fails when credentials are missing", func() {
		_, err := New("", "", &fakeFetcher{}).Fetch(context.Background())
		Expect(err).To(HaveOccurred())
	})

	It("propagates account fetch failure", func() {
		c := New("k", "s", &fakeFetcher{balErr: errors.New("boom")})
		_, err := c.Fetch(context.Background())
		Expect(err).To(MatchError(ContainSubstring("boom")))
	})

	It("translates BTC/USDT into a position priced in USD", func() {
		c := New("k", "s", &fakeFetcher{
			balances: []RawBalance{
				{Asset: "BTC", Free: 0.5, Locked: 0},
				{Asset: "USDT", Free: 1000},
			},
			prices: map[string]float64{"BTCUSDT": 60000},
		})
		snap, err := c.Fetch(context.Background())
		Expect(err).NotTo(HaveOccurred())
		Expect(snap.Connection.BrokerageSlug).To(Equal("binance"))
		Expect(snap.Holdings[0].Balances[0].Cash).To(Equal(1000.0)) // USDT folded as cash
		Expect(snap.Holdings[0].Positions).To(HaveLen(1))
		Expect(snap.Holdings[0].Positions[0].Symbol.Symbol).To(Equal("BTC"))
		Expect(snap.Holdings[0].Positions[0].Units).To(Equal(0.5))
	})

	It("falls back to empty prices when ticker fetch fails", func() {
		c := New("k", "s", &fakeFetcher{
			balances: []RawBalance{
				{Asset: "USDC", Free: 200},
				{Asset: "BTC", Free: 0.1},
			},
			priceErr: errors.New("rate limit"),
		})
		snap, err := c.Fetch(context.Background())
		Expect(err).NotTo(HaveOccurred())
		// USDC still gets folded as cash; BTC has zero USD value so it's
		// dropped by the dust filter.
		Expect(snap.Holdings[0].Balances[0].Cash).To(Equal(200.0))
		Expect(snap.Holdings[0].Positions).To(BeEmpty())
	})

	It("skips assets with zero combined quantity", func() {
		c := New("k", "s", &fakeFetcher{
			balances: []RawBalance{
				{Asset: "ETH", Free: 0, Locked: 0}, // dropped before pricing
				{Asset: "USDT", Free: 50},
			},
		})
		snap, err := c.Fetch(context.Background())
		Expect(err).NotTo(HaveOccurred())
		Expect(snap.Holdings[0].Balances[0].Cash).To(Equal(50.0))
		Expect(snap.Holdings[0].Positions).To(BeEmpty())
	})

	It("uses the real SDK fetcher when nil is passed", func() {
		// We cannot reach the network in unit tests, but we can confirm
		// New(...) constructs a client successfully and the missing-cred
		// check runs first, exercising the nil-fetcher branch.
		_, err := New("", "", nil).Fetch(context.Background())
		Expect(err).To(HaveOccurred())
	})

	It("fetches trade history into Binance activities and marks the account synced", func() {
		fetcher := &fakeFetcher{
			balances: []RawBalance{{Asset: "BTC", Free: 1}},
			prices:   map[string]float64{"BTCUSDT": 60000},
			trades: map[string][]UserTrade{"BTCUSDT": {
				{ID: 41, Symbol: "BTCUSDT", Price: "60000.25", Qty: "0.1", Commission: "0.001", CommissionAsset: "BTC", TimeMs: 1700000000123, IsBuyer: true},
				{ID: 42, Symbol: "BTCUSDT", Price: "61000", Qty: "0.2", Commission: "0.25", CommissionAsset: "USDT", TimeMs: 1700000001123},
			}},
		}
		snapshot, err := New("k", "s", fetcher).Fetch(context.Background())
		Expect(err).NotTo(HaveOccurred())
		Expect(snapshot.Accounts[0].InitialTxSyncDone).To(BeTrue())

		activities := snapshot.Activities["binance-spot"]
		Expect(activities).To(HaveLen(2))
		Expect(activities[0].ID).To(Equal("41"))
		Expect(activities[0].SourceRecordID).To(Equal("41"))
		Expect(activities[0].Type).To(Equal(brokerage.ActivityBuy))
		Expect(activities[0].RawType).To(Equal("BUY"))
		Expect(activities[0].Price).To(Equal(60000.25))
		Expect(activities[0].Units).To(Equal(0.1))
		Expect(activities[0].Fee).To(Equal(0.001))
		Expect(activities[0].Symbol.Symbol).To(Equal("BTCUSDT"))
		Expect(activities[0].TradeDate.UnixMilli()).To(Equal(int64(1700000000123)))
		Expect(activities[1].Type).To(Equal(brokerage.ActivitySell))
		Expect(activities[1].Fee).To(Equal(0.25))
	})

	It("keeps the balances snapshot when trade history fails", func() {
		snapshot, err := New("k", "s", &fakeFetcher{
			balances: []RawBalance{{Asset: "BTC", Free: 1}},
			tradeErr: errors.New("history unavailable"),
		}).Fetch(context.Background())
		Expect(err).NotTo(HaveOccurred())
		Expect(snapshot.Activities).To(BeEmpty())
		Expect(snapshot.Accounts[0].InitialTxSyncDone).To(BeFalse())
	})

	It("maps Binance user-trade fields into the shared trade shape", func() {
		buy := mapTrade(UserTrade{
			ID: 7001, Symbol: "ETHUSDT", Price: "12.5", Qty: "2.25",
			Commission: "0.03", CommissionAsset: "BNB", TimeMs: 1700000000123, IsBuyer: true,
		})
		Expect(buy.ID).To(Equal("7001"))
		Expect(buy.Symbol).To(Equal("ETHUSDT"))
		Expect(buy.Side).To(Equal("buy"))
		Expect(buy.Price).To(Equal(12.5))
		Expect(buy.Quantity).To(Equal(2.25))
		Expect(buy.Fee).To(Equal(0.03))
		Expect(buy.FeeAsset).To(Equal("BNB"))
		Expect(buy.Timestamp.UnixMilli()).To(Equal(int64(1700000000123)))

		sell := mapTrade(UserTrade{IsBuyer: false})
		Expect(sell.Side).To(Equal("sell"))

		malformed := mapTrade(UserTrade{Price: "bad", Qty: "bad", Commission: "bad"})
		Expect(malformed.Price).To(BeZero())
		Expect(malformed.Quantity).To(BeZero())
		Expect(malformed.Fee).To(BeZero())
	})

	It("queries each distinct non-stablecoin held asset once", func() {
		fetcher := &fakeFetcher{balances: []RawBalance{
			{Asset: "btc", Free: 1},
			{Asset: "BTC", Locked: 2},
			{Asset: "USDT", Free: 100},
			{Asset: "USDC", Free: 50},
			{Asset: "ETH"},
		}}
		_, err := New("k", "s", fetcher).Fetch(context.Background())
		Expect(err).NotTo(HaveOccurred())
		Expect(fetcher.tradeCalls).To(Equal([]tradeCall{{symbol: "BTCUSDT", endTimeMs: 0}}))
	})

	It("stops pagination after a full page followed by a short page", func() {
		firstPage := make([]UserTrade, tradePageSize)
		for i := range firstPage {
			firstPage[i] = UserTrade{ID: int64(i + 1), Symbol: "ETHUSDT", Price: "1", Qty: "1", IsBuyer: true, TimeMs: 2000 + int64(i)}
		}
		fetcher := &fakeFetcher{
			balances: []RawBalance{{Asset: "ETH", Free: 1}},
			tradePages: map[string]map[int64][]UserTrade{
				"ETHUSDT": {
					0:    firstPage,
					1999: {{ID: 1001, Symbol: "ETHUSDT", TimeMs: 999}, {ID: 1002, Symbol: "ETHUSDT", TimeMs: 998}},
				},
			},
		}
		snapshot, err := New("k", "s", fetcher).Fetch(context.Background())
		Expect(err).NotTo(HaveOccurred())
		Expect(fetcher.tradeCalls).To(Equal([]tradeCall{
			{symbol: "ETHUSDT", endTimeMs: 0},
			{symbol: "ETHUSDT", endTimeMs: 1999},
		}))
		Expect(snapshot.Activities["binance-spot"]).To(HaveLen(1002))
	})

	It("stops pagination when a page comes back empty", func() {
		fetcher := &fakeFetcher{
			balances: []RawBalance{{Asset: "ETH", Free: 1}},
			tradePages: map[string]map[int64][]UserTrade{
				"ETHUSDT": {
					0:    {},
					1999: {{ID: 1001, Symbol: "ETHUSDT"}},
				},
			},
		}
		_, err := New("k", "s", fetcher).Fetch(context.Background())
		Expect(err).NotTo(HaveOccurred())
		Expect(fetcher.tradeCalls).To(Equal([]tradeCall{{symbol: "ETHUSDT", endTimeMs: 0}}))
	})

	It("imports completed Buy Crypto fiat payments in their source currency", func() {
		fetcher := &fakeFetcher{fiatPages: map[string]map[int32]FiatPaymentPage{
			"0": {1: {Success: true, Total: 3, Data: []FiatPayment{
				{
					OrderNo: "buy-1", SourceAmount: "1000", FiatCurrency: "php",
					ObtainAmount: "2.5", CryptoCurrency: "sol", TotalFee: "10", Price: "400",
					Status: "cOmPlEtEd", CreateTime: 1700000000123,
				},
				{
					OrderNo: "buy-processing", SourceAmount: "2000", FiatCurrency: "PHP",
					ObtainAmount: "5", CryptoCurrency: "SOL", TotalFee: "20", Price: "400",
					Status: "Processing", CreateTime: 1700000001123,
				},
				{
					OrderNo: "buy-failed", SourceAmount: "3000", FiatCurrency: "PHP",
					ObtainAmount: "7.5", CryptoCurrency: "SOL", TotalFee: "30", Price: "400",
					Status: "Failed", CreateTime: 1700000002123,
				},
			}}},
		}}
		snapshot, err := New("k", "s", fetcher).Fetch(context.Background())
		Expect(err).NotTo(HaveOccurred())
		Expect(snapshot.Accounts[0].InitialTxSyncDone).To(BeTrue())

		activities := snapshot.Activities["binance-spot"]
		Expect(activities).To(HaveLen(1))
		Expect(activities[0].ID).To(Equal("fiat:buy-1"))
		Expect(activities[0].SourceRecordID).To(Equal("fiat:buy-1"))
		Expect(activities[0].Type).To(Equal(brokerage.ActivityBuy))
		Expect(activities[0].Symbol.Symbol).To(Equal("SOL"))
		Expect(activities[0].Price).To(Equal(400.0))
		Expect(activities[0].Units).To(Equal(2.5))
		Expect(activities[0].Amount).To(Equal(1000.0))
		Expect(activities[0].Fee).To(Equal(10.0))
		Expect(activities[0].Currency.Code).To(Equal("PHP"))
		Expect(activities[0].Symbol.Currency.Code).To(Equal("PHP"))
		Expect(activities[0].TradeDate.UnixMilli()).To(Equal(int64(1700000000123)))
	})

	It("marks successful fiat history fetched even when every order is incomplete", func() {
		fetcher := &fakeFetcher{fiatPages: map[string]map[int32]FiatPaymentPage{
			"0": {1: {Success: true, Total: 1, Data: []FiatPayment{{Status: "Processing"}}}},
		}}
		snapshot, err := New("k", "s", fetcher).Fetch(context.Background())
		Expect(err).NotTo(HaveOccurred())
		Expect(snapshot.Activities).To(BeEmpty())
		Expect(snapshot.Accounts[0].InitialTxSyncDone).To(BeTrue())
	})

	It("paginates fiat history within the bounded page size", func() {
		firstPage := make([]FiatPayment, fiatPaymentPageSize)
		for i := range firstPage {
			firstPage[i].Status = "Processing"
		}
		fetcher := &fakeFetcher{fiatPages: map[string]map[int32]FiatPaymentPage{
			"0": {
				1: {Success: true, Total: 101, Data: firstPage},
				2: {Success: true, Total: 101, Data: []FiatPayment{{
					OrderNo: "last", SourceAmount: "100", FiatCurrency: "PHP", ObtainAmount: "1",
					CryptoCurrency: "SOL", Price: "100", Status: "Completed",
				}}},
			},
		}}
		snapshot, err := New("k", "s", fetcher).Fetch(context.Background())
		Expect(err).NotTo(HaveOccurred())
		Expect(snapshot.Activities["binance-spot"]).To(HaveLen(1))
		Expect(fetcher.fiatCalls).To(Equal([]fiatPaymentCall{
			{transactionType: "0", page: 1, rows: fiatPaymentPageSize},
			{transactionType: "0", page: 2, rows: fiatPaymentPageSize},
		}))
	})

	It("leaves transaction sync incomplete when fiat history exceeds the fetch cap", func() {
		fetcher := &fakeFetcher{fiatPages: map[string]map[int32]FiatPaymentPage{
			"0": {1: {Success: true, Total: fiatPaymentMaxRows + 1}},
		}}
		snapshot, err := New("k", "s", fetcher).Fetch(context.Background())
		Expect(err).NotTo(HaveOccurred())
		Expect(snapshot.Activities).To(BeEmpty())
		Expect(snapshot.Accounts[0].InitialTxSyncDone).To(BeFalse())
		Expect(fetcher.fiatCalls).To(HaveLen(1))
	})

	It("keeps the balances snapshot when fiat history fails", func() {
		snapshot, err := New("k", "s", &fakeFetcher{
			balances: []RawBalance{{Asset: "SOL", Free: 1}},
			fiatErr:  errors.New("fiat history unavailable"),
		}).Fetch(context.Background())
		Expect(err).NotTo(HaveOccurred())
		Expect(snapshot.Holdings).To(HaveLen(1))
		Expect(snapshot.Activities).To(BeEmpty())
		Expect(snapshot.Accounts[0].InitialTxSyncDone).To(BeFalse())
	})

	It("maps the fiat fee asset and prefixed order ID", func() {
		trade := mapFiatPayment(FiatPayment{
			OrderNo: "order-1", SourceAmount: "30", FiatCurrency: "php",
			ObtainAmount: "1", CryptoCurrency: "sol", TotalFee: "0.5", Price: "30",
			CreateTime: 1700000000123,
		})
		Expect(trade.ID).To(Equal("fiat:order-1"))
		Expect(trade.Symbol).To(Equal("SOL"))
		Expect(trade.FeeAsset).To(Equal("PHP"))
		Expect(trade.Currency).To(Equal("PHP"))
		Expect(trade.Amount).To(Equal(30.0))
	})
})
