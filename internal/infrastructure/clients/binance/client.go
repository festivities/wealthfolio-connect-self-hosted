// Package binance implements a BrokerClient that reads spot account
// balances from Binance via the official adshao/go-binance/v2 SDK.
//
// To compute USD valuation we hit the public /api/v3/ticker/price endpoint
// once per non-stable asset using its USDT pair (BTCUSDT, ETHUSDT, ...).
// Stablecoins skip the price lookup and are folded into the cash balance.
package binance

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	binsdk "github.com/adshao/go-binance/v2"

	domainsync "github.com/wealthfolio/wealthfolio-connect-self-hosted/internal/domain/sync"
	"github.com/wealthfolio/wealthfolio-connect-self-hosted/internal/infrastructure/clients/cexcommon"
)

// Fetcher abstracts the Binance SDK so tests can plug in a fake.
type Fetcher interface {
	// Account fetches the user's non-zero spot balances.
	Account(ctx context.Context) ([]RawBalance, error)
	// Prices fetches current prices keyed by symbol, such as BTCUSDT.
	Prices(ctx context.Context) (map[string]float64, error)
	// Trades fetches one page of the user's spot trades for a symbol,
	// returning the most recent trades up to endTimeMs (0 = unbounded).
	Trades(ctx context.Context, symbol string, endTimeMs int64) ([]UserTrade, error)
	// FiatPayments fetches one page of the user's fiat payment history.
	FiatPayments(ctx context.Context, transactionType string, page, rows int32) (FiatPaymentPage, error)
}

// RawBalance is the per-asset payload a Fetcher returns. Public so tests
// can construct it.
type RawBalance struct {
	Asset  string
	Free   float64
	Locked float64
}

// UserTrade is one Binance Spot trade in an SDK-independent shape.
type UserTrade struct {
	// ID is Binance's trade ID.
	ID int64
	// Symbol is the raw trading pair, such as BTCUSDT.
	Symbol string
	// Price is the trade price as returned by Binance.
	Price string
	// Qty is the traded quantity as returned by Binance.
	Qty string
	// Commission is the fee amount as returned by Binance.
	Commission string
	// CommissionAsset is the asset used to charge the fee.
	CommissionAsset string
	// TimeMs is the trade timestamp in Unix milliseconds.
	TimeMs int64
	// IsBuyer indicates whether this account was the buyer.
	IsBuyer bool
}

// FiatPayment is one Binance fiat payment in an SDK-independent shape.
type FiatPayment struct {
	// OrderNo is Binance's fiat payment order identifier.
	OrderNo string
	// SourceAmount is the amount paid or received in fiat currency.
	SourceAmount string
	// FiatCurrency is the currency used for the source amount and fee.
	FiatCurrency string
	// ObtainAmount is the crypto amount obtained by the order.
	ObtainAmount string
	// CryptoCurrency is the asset obtained by the order.
	CryptoCurrency string
	// TotalFee is the order fee in FiatCurrency.
	TotalFee string
	// Price is the fiat price per crypto unit.
	Price string
	// Status is the exchange's order status.
	Status string
	// CreateTime is the order creation time in Unix milliseconds.
	CreateTime int64
}

// FiatPaymentPage is one page of Binance fiat payment history.
type FiatPaymentPage struct {
	// Success reports whether Binance accepted the request.
	Success bool
	// Total is the total number of rows for this transaction type.
	Total int32
	// Data contains the returned payment rows.
	Data []FiatPayment
}

const (
	tradePageSize = 1000
	tradeMaxPages = 2

	fiatPaymentPageSize int32 = 100
	fiatPaymentMaxPages int32 = 5
	fiatPaymentMaxRows        = fiatPaymentPageSize * fiatPaymentMaxPages
)

// Client is the Binance BrokerClient.
type Client struct {
	apiKey, secret string
	fetcher        Fetcher
}

// New builds a client. Pass nil fetcher to use the real SDK.
func New(apiKey, secret string, f Fetcher) *Client {
	if f == nil {
		f = &realFetcher{client: binsdk.NewClient(apiKey, secret)}
	}
	return &Client{apiKey: apiKey, secret: secret, fetcher: f}
}

// ID returns the slug used by sync orchestration.
func (c *Client) ID() string { return "binance" }

// Fetch pulls account balances, USDT-quoted prices, and recent trade history.
func (c *Client) Fetch(ctx context.Context) (domainsync.BrokerSnapshot, error) {
	if c.apiKey == "" || c.secret == "" {
		return domainsync.BrokerSnapshot{}, errors.New("binance: api key/secret not configured")
	}
	balances, err := c.fetcher.Account(ctx)
	if err != nil {
		return domainsync.BrokerSnapshot{}, fmt.Errorf("binance: account: %w", err)
	}
	prices, err := c.fetcher.Prices(ctx)
	if err != nil {
		// Prices are best-effort: without them positions just have zero
		// USD valuation and most will be filtered out by the dust filter,
		// but cash stablecoins still flow through.
		prices = map[string]float64{}
	}
	snapshot := buildSnapshot(balances, prices)
	spotTrades, spotErr := c.fetchTrades(ctx, balances)
	fiatTrades, fiatErr := c.fetchFiatPayments(ctx)
	if spotErr == nil && fiatErr == nil {
		snapshot.Trades = append(spotTrades, fiatTrades...)
		snapshot.ActivitiesFetched = true
	}
	return cexcommon.Translate("binance", "Binance", snapshot), nil
}

func (c *Client) fetchFiatPayments(ctx context.Context) ([]cexcommon.Trade, error) {
	// Sell payments reverse the source/obtain currencies. Their mapping needs
	// separate treatment; only import Buy Crypto orders here.
	var all []cexcommon.Trade
	first, err := c.fetcher.FiatPayments(ctx, string(binsdk.TransactionTypeBuy), 1, fiatPaymentPageSize)
	if err != nil {
		return nil, fmt.Errorf("binance: fiat buy history: %w", err)
	}
	if !first.Success {
		return nil, errors.New("binance: fiat buy history request unsuccessful")
	}
	if first.Total < 0 || first.Total > fiatPaymentMaxRows {
		return nil, fmt.Errorf("binance: fiat buy history exceeds the %d-row sync limit", fiatPaymentMaxRows)
	}

	pages := (first.Total + fiatPaymentPageSize - 1) / fiatPaymentPageSize
	if pages == 0 {
		pages = 1
	}
	for page := int32(1); page <= pages; page++ {
		current := first
		if page > 1 {
			current, err = c.fetcher.FiatPayments(ctx, string(binsdk.TransactionTypeBuy), page, fiatPaymentPageSize)
			if err != nil {
				return nil, fmt.Errorf("binance: fiat buy history page %d: %w", page, err)
			}
		}
		if !current.Success {
			return nil, fmt.Errorf("binance: fiat buy history page %d unsuccessful", page)
		}
		if current.Total != first.Total {
			return nil, errors.New("binance: fiat buy history changed while paging")
		}
		expectedRows := fiatPaymentPageSize
		remaining := first.Total - (page-1)*fiatPaymentPageSize
		if remaining < expectedRows {
			expectedRows = remaining
		}
		if int32(len(current.Data)) != expectedRows {
			return nil, fmt.Errorf("binance: fiat buy history page %d incomplete", page)
		}
		for _, payment := range current.Data {
			if strings.EqualFold(strings.TrimSpace(payment.Status), "completed") {
				all = append(all, mapFiatPayment(payment))
			}
		}
	}
	return all, nil
}

func mapFiatPayment(payment FiatPayment) cexcommon.Trade {
	price, _ := strconv.ParseFloat(payment.Price, 64)           //nolint:errcheck // treat malformed exchange values as zero
	quantity, _ := strconv.ParseFloat(payment.ObtainAmount, 64) //nolint:errcheck // treat malformed exchange values as zero
	amount, _ := strconv.ParseFloat(payment.SourceAmount, 64)   //nolint:errcheck // treat malformed exchange values as zero
	fee, _ := strconv.ParseFloat(payment.TotalFee, 64)          //nolint:errcheck // treat malformed exchange values as zero
	currency := strings.ToUpper(strings.TrimSpace(payment.FiatCurrency))
	if currency != "USD" {
		// The official Wealthfolio consumer seeds a broker quote from a positive
		// unit price without checking that its currency matches the asset quote.
		// Preserve its prior amount fallback before suppressing this incompatible
		// price; amount, fee, units and source identity remain exchange-derived.
		if amount == 0 {
			amount = price * quantity
		}
		price = 0
	}
	return cexcommon.Trade{
		ID:        "fiat:" + payment.OrderNo,
		Symbol:    strings.ToUpper(payment.CryptoCurrency),
		Side:      "buy",
		Price:     price,
		Quantity:  quantity,
		Fee:       fee,
		FeeAsset:  currency,
		Timestamp: time.UnixMilli(payment.CreateTime).UTC(),
		Currency:  currency,
		// Binance Spot holdings are quoted in USD; keep fiat transaction currency
		// separate so purchases resolve to the same crypto asset. Non-USD fiat
		// prices are omitted because the consumer would misapply them as USD quotes.
		SymbolCurrency: "USD",
		Amount:         amount,
	}
}

// fetchTrades walks backwards through each symbol's trade history. Binance
// returns the most recent trades when no filter is sent, and the most recent
// trades up to endTime when endTime is set, so each full page advances the
// cursor below the oldest trade it contains. Bounded to tradeMaxPages per
// symbol; persistence upserts deduplicate any overlap.
func (c *Client) fetchTrades(ctx context.Context, balances []RawBalance) ([]cexcommon.Trade, error) {
	var all []cexcommon.Trade
	for _, symbol := range tradeSymbols(balances) {
		endTimeMs := int64(0)
		for page := 0; page < tradeMaxPages; page++ {
			trades, err := c.fetcher.Trades(ctx, symbol, endTimeMs)
			if err != nil {
				return nil, fmt.Errorf("binance: trades %s: %w", symbol, err)
			}
			if len(trades) == 0 {
				break
			}
			for _, trade := range trades {
				all = append(all, mapTrade(trade))
			}
			if len(trades) < tradePageSize {
				break
			}
			// Binance returns pages in chronological order, so the first
			// trade is the page's oldest. Step below it for the next page.
			endTimeMs = trades[0].TimeMs - 1
		}
	}
	return all, nil
}

func tradeSymbols(balances []RawBalance) []string {
	symbols := make([]string, 0, len(balances))
	seen := make(map[string]struct{}, len(balances))
	for _, balance := range balances {
		if balance.Free+balance.Locked == 0 || cexcommon.IsStablecoin(balance.Asset) {
			continue
		}
		symbol := strings.ToUpper(balance.Asset) + "USDT"
		if _, ok := seen[symbol]; ok {
			continue
		}
		seen[symbol] = struct{}{}
		symbols = append(symbols, symbol)
	}
	return symbols
}

func mapTrade(trade UserTrade) cexcommon.Trade {
	price, _ := strconv.ParseFloat(trade.Price, 64)    //nolint:errcheck // treat malformed exchange values as zero
	quantity, _ := strconv.ParseFloat(trade.Qty, 64)   //nolint:errcheck // treat malformed exchange values as zero
	fee, _ := strconv.ParseFloat(trade.Commission, 64) //nolint:errcheck // treat malformed exchange values as zero
	side := "sell"
	if trade.IsBuyer {
		side = "buy"
	}
	return cexcommon.Trade{
		ID:        strconv.FormatInt(trade.ID, 10),
		Symbol:    trade.Symbol,
		Side:      side,
		Price:     price,
		Quantity:  quantity,
		Fee:       fee,
		FeeAsset:  trade.CommissionAsset,
		Timestamp: time.UnixMilli(trade.TimeMs).UTC(),
	}
}

// buildSnapshot is exposed via BuildSnapshotForTest so external tests can
// drive the mapping pipeline.
func buildSnapshot(balances []RawBalance, prices map[string]float64) cexcommon.Snapshot {
	out := cexcommon.Snapshot{}
	for _, b := range balances {
		qty := b.Free + b.Locked
		if qty == 0 {
			continue
		}
		asset := strings.ToUpper(b.Asset)
		var price, usd float64
		if cexcommon.IsStablecoin(asset) {
			price = 1
			usd = qty
		} else {
			price = prices[asset+"USDT"]
			usd = price * qty
		}
		out.Balances = append(out.Balances, cexcommon.Balance{
			Asset:    asset,
			Quantity: qty,
			PriceUSD: price,
			USDValue: usd,
		})
	}
	return out
}

// ===================== real Binance SDK fetcher =====================

type realFetcher struct {
	client *binsdk.Client
}

// Account fetches all non-zero balances from the user's Binance spot account.
func (f *realFetcher) Account(ctx context.Context) ([]RawBalance, error) {
	acc, err := f.client.NewGetAccountService().OmitZeroBalances(true).Do(ctx)
	if err != nil {
		return nil, err
	}
	out := make([]RawBalance, 0, len(acc.Balances))
	for _, b := range acc.Balances {
		free, _ := strconv.ParseFloat(b.Free, 64)     //nolint:errcheck // SDK returns well-formed numeric strings; treat unparsable values as zero
		locked, _ := strconv.ParseFloat(b.Locked, 64) //nolint:errcheck // SDK returns well-formed numeric strings; treat unparsable values as zero
		if free == 0 && locked == 0 {
			continue
		}
		out = append(out, RawBalance{Asset: b.Asset, Free: free, Locked: locked})
	}
	return out, nil
}

// Prices returns a snapshot of every symbol's last traded price keyed by symbol.
func (f *realFetcher) Prices(ctx context.Context) (map[string]float64, error) {
	prices, err := f.client.NewListPricesService().Do(ctx)
	if err != nil {
		return nil, err
	}
	out := make(map[string]float64, len(prices))
	for _, p := range prices {
		v, err := strconv.ParseFloat(p.Price, 64)
		if err == nil {
			out[p.Symbol] = v
		}
	}
	return out, nil
}

// Trades fetches a page of the account's Spot trades for one symbol,
// most recent first up to endTimeMs (0 = no bound).
func (f *realFetcher) Trades(ctx context.Context, symbol string, endTimeMs int64) ([]UserTrade, error) {
	svc := f.client.NewListTradesService().Symbol(symbol).Limit(tradePageSize)
	// An explicit fromId anchors the query at the OLDEST trades, so it is
	// never sent; endTime alone pages backwards through recent history.
	if endTimeMs > 0 {
		svc = svc.EndTime(endTimeMs)
	}
	trades, err := svc.Do(ctx)
	if err != nil {
		return nil, err
	}
	out := make([]UserTrade, 0, len(trades))
	for _, trade := range trades {
		if trade == nil {
			continue
		}
		out = append(out, UserTrade{
			ID:              trade.ID,
			Symbol:          trade.Symbol,
			Price:           trade.Price,
			Qty:             trade.Quantity,
			Commission:      trade.Commission,
			CommissionAsset: trade.CommissionAsset,
			TimeMs:          trade.Time,
			IsBuyer:         trade.IsBuyer,
		})
	}
	return out, nil
}

// FiatPayments fetches one page of fiat payment history from Binance.
func (f *realFetcher) FiatPayments(ctx context.Context, transactionType string, page, rows int32) (FiatPaymentPage, error) {
	history, err := f.client.NewFiatPaymentsHistoryService().
		TransactionType(binsdk.TransactionType(transactionType)).
		Page(page).
		Rows(rows).
		Do(ctx)
	if err != nil {
		return FiatPaymentPage{}, err
	}
	if history == nil {
		return FiatPaymentPage{}, errors.New("binance: empty fiat payment history response")
	}
	data := make([]FiatPayment, 0, len(history.Data))
	for _, payment := range history.Data {
		data = append(data, FiatPayment{
			OrderNo:        payment.OrderNo,
			SourceAmount:   payment.SourceAmount,
			FiatCurrency:   payment.FiatCurrency,
			ObtainAmount:   payment.ObtainAmount,
			CryptoCurrency: payment.CryptoCurrency,
			TotalFee:       payment.TotalFee,
			Price:          payment.Price,
			Status:         payment.Status,
			CreateTime:     payment.CreateTime,
		})
	}
	return FiatPaymentPage{Success: history.Success, Total: history.Total, Data: data}, nil
}
