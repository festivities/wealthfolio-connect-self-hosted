// Package cexcommon contains the shared translation helpers used by every
// CEX client (Binance, OKX, Bitget, Hyperliquid). Each individual client
// only needs to fetch raw balances and let cexcommon turn them into a
// BrokerSnapshot using a uniform set of conventions:
//
//   - Stablecoins (USDT, USDC, DAI, ...) collapse into the cash balance.
//   - Tiny dust positions (USD value < $1) are dropped.
//   - One brokerage Account per exchange (slug + "-spot").
package cexcommon

import (
	"math"
	"sort"
	"strings"
	"time"

	"github.com/wealthfolio/wealthfolio-connect-self-hosted/internal/domain/brokerage"
	domainsync "github.com/wealthfolio/wealthfolio-connect-self-hosted/internal/domain/sync"
)

// Balance is the normalised per-asset row that each CEX client produces.
type Balance struct {
	Asset    string  // e.g. "BTC"
	Quantity float64 // free + locked
	PriceUSD float64
	USDValue float64 // priceUSD * quantity, supplied by the upstream when available
}

// Trade is one historical trade row. Optional — most CEX clients only fill
// balances on the initial pass.
type Trade struct {
	ID        string
	Symbol    string // e.g. "BTC-USDT"
	Side      string // "buy" or "sell"
	Price     float64
	Quantity  float64
	Fee       float64
	FeeAsset  string
	Timestamp time.Time
	Currency  string  // optional activity currency; defaults to USD
	Amount    float64 // optional activity amount; defaults to Price * Quantity
	// SymbolCurrency optionally overrides the asset's quote currency. This can
	// differ from the transaction currency for a fiat-funded crypto purchase.
	SymbolCurrency string
}

// Snapshot bundles balances + optional trades for one CEX.
type Snapshot struct {
	Balances          []Balance
	Trades            []Trade
	ActivitiesFetched bool
}

// Translate folds a CEX snapshot into a BrokerSnapshot. The resulting
// connection/account IDs are derived from the supplied slug ("okx",
// "binance", ...) so callers can produce stable rows.
func Translate(slug, displayName string, s Snapshot) domainsync.BrokerSnapshot {
	now := time.Now().UTC()
	accountID := slug + "-spot"

	var totalUSD float64
	for _, b := range s.Balances {
		totalUSD += b.USDValue
	}

	account := brokerage.Account{
		ID:                     accountID,
		Name:                   displayName + " Spot",
		Type:                   brokerage.AccountTypeCryptocurrency,
		RawType:                "CRYPTO_SPOT",
		Currency:               "USD",
		BalanceTotal:           totalUSD,
		BalanceCurrency:        "USD",
		BrokerageAuthorization: slug + "-auth",
		InstitutionName:        displayName,
		SyncEnabled:            true,
		Status:                 "open",
		CreatedDate:            now,
		LastHoldingsSync:       &now,
		InitialTxSyncDone:      s.ActivitiesFetched,
		InitialHoldingsDone:    true,
	}
	if s.ActivitiesFetched {
		account.LastTxSync = &now
	}

	connection := brokerage.Connection{
		ID:              slug + "-conn",
		AuthorizationID: slug + "-auth",
		BrokerageName:   displayName,
		BrokerageSlug:   slug,
		DisplayName:     displayName,
		Name:            displayName,
		Status:          brokerage.ConnectionActive,
		UpdatedAt:       now,
	}

	averageCosts := averageCostByAsset(s.Balances, s.Trades)

	cashBalance := brokerage.Balance{
		Currency: brokerage.Currency{Code: "USD"},
	}
	positions := make([]brokerage.Position, 0, len(s.Balances))
	for _, b := range s.Balances {
		if b.Quantity == 0 || b.USDValue < 1 {
			continue
		}
		asset := strings.ToUpper(b.Asset)
		if IsStablecoin(asset) {
			cashBalance.Cash += b.USDValue
			continue
		}
		positions = append(positions, brokerage.Position{
			Symbol: brokerage.Symbol{
				Symbol:      asset,
				RawSymbol:   asset,
				Description: asset,
				Name:        asset,
				Type:        brokerage.SymbolType{Code: "CRYPTO", IsSupported: true, Description: "Cryptocurrency"},
				Exchange:    brokerage.Exchange{Code: strings.ToUpper(slug), Name: displayName},
				Currency:    brokerage.Currency{Code: "USD"},
			},
			Units: b.Quantity,
			Price: b.PriceUSD,
			// The balance only exposes the current mark, never the cost. When the
			// fetched trade history fully explains the position we derive the true
			// average cost from it; otherwise the value stays zero so callers omit
			// it rather than present the mark (or a bare zero) as the cost basis.
			AveragePurchasePrice: averageCosts[asset],
			Currency:             brokerage.Currency{Code: "USD"},
		})
	}

	holding := brokerage.Holdings{
		AccountID:  accountID,
		Balances:   []brokerage.Balance{cashBalance},
		Positions:  positions,
		CapturedAt: now,
	}

	activities := map[string][]brokerage.Activity{}
	if len(s.Trades) > 0 {
		acts := make([]brokerage.Activity, 0, len(s.Trades))
		for _, t := range s.Trades {
			actType := brokerage.ActivityBuy
			if strings.EqualFold(t.Side, "sell") {
				actType = brokerage.ActivitySell
			}
			currency := t.Currency
			if currency == "" {
				currency = "USD"
			}
			symbolCurrency := t.SymbolCurrency
			if symbolCurrency == "" {
				symbolCurrency = currency
			}
			amount := t.Amount
			if amount == 0 {
				amount = t.Price * t.Quantity
			}
			acts = append(acts, brokerage.Activity{
				ID:        t.ID,
				AccountID: accountID,
				Type:      actType,
				TradeDate: t.Timestamp,
				Price:     t.Price,
				Units:     t.Quantity,
				Amount:    amount,
				Fee:       t.Fee,
				Currency:  brokerage.Currency{Code: currency},
				Symbol: &brokerage.Symbol{
					Symbol:    t.Symbol,
					RawSymbol: t.Symbol,
					Type:      brokerage.SymbolType{Code: "CRYPTO", IsSupported: true},
					Exchange:  brokerage.Exchange{Code: strings.ToUpper(slug)},
					Currency:  brokerage.Currency{Code: symbolCurrency},
				},
				RawType:        strings.ToUpper(t.Side),
				ProviderType:   slug,
				SourceSystem:   slug,
				SourceRecordID: t.ID,
			})
		}
		activities[accountID] = acts
	}

	return domainsync.BrokerSnapshot{
		Connection: connection,
		Accounts:   []brokerage.Account{account},
		Holdings:   []brokerage.Holdings{holding},
		Activities: activities,
	}
}

// quantityRelTolerance guards sums of exchange-supplied float64 quantities
// against accumulated rounding. A single float64 carries ~2e-16 relative
// precision, so summing a few thousand fills stays well inside 1e-12 relative;
// 1e-9 is generous yet still rejects any balance off by a meaningful fraction.
// An absolute floor covers reference values near zero. Using a relative bound
// (rather than an absolute 1e-8) matters for tiny crypto balances, where an
// absolute tolerance would swallow a materially different balance and report a
// false "known basis".
const (
	quantityRelTolerance = 1e-9
	quantityAbsFloor     = 1e-12
)

func quantityTolerance(reference float64) float64 {
	return math.Max(quantityAbsFloor, quantityRelTolerance*math.Abs(reference))
}

// averageCostByAsset derives a per-asset average purchase price in USD from the
// trade history using average-cost accounting. It returns a value only when the
// entire current balance is explained by USD-denominated trades; a position that
// has any non-USD fill, predates the fetched history, or was partly built by an
// unrecorded transfer is left out so no fabricated cost basis is reported.
//
// Fees are ignored: commission can be charged in an unrelated asset, and folding
// it in would need a price for that asset. Treating USDT/USDC fills as USD is the
// same assumption the USD valuation already makes.
//
// Known limitation: trade history contains no transfers. A deposit (or an
// offsetting deposit+withdrawal) that nets the balance to the trade total is
// indistinguishable from a fully traded position and will be costed as if it had
// been bought, so the derived value is best-effort, not authoritative.
func averageCostByAsset(balances []Balance, trades []Trade) map[string]float64 {
	assets := make([]string, 0, len(balances))
	held := make(map[string]float64, len(balances))
	for _, b := range balances {
		asset := strings.ToUpper(b.Asset)
		if b.Quantity == 0 || IsStablecoin(asset) {
			continue
		}
		if _, ok := held[asset]; !ok {
			assets = append(assets, asset)
		}
		held[asset] += b.Quantity
	}
	if len(assets) == 0 {
		return nil
	}
	// Longest ticker first so "BTC" matches before a shorter ticker it prefixes.
	sort.Slice(assets, func(i, j int) bool { return len(assets[i]) > len(assets[j]) })

	type ledger struct {
		quantity float64
		cost     float64
		unknown  bool
	}
	ledgers := make(map[string]*ledger, len(assets))
	// A fill outside the USD quote (e.g. a PHP-funded purchase) added units whose
	// cost we cannot convert, so the asset's basis is unknowable even if USD
	// trades later happen to net the same quantity.
	foreign := make(map[string]struct{})

	// Chronological order keeps the running average well-defined when a sell
	// appears before a buy only because the history was trimmed.
	ordered := make([]Trade, len(trades))
	copy(ordered, trades)
	sort.SliceStable(ordered, func(i, j int) bool {
		return ordered[i].Timestamp.Before(ordered[j].Timestamp)
	})

	for _, t := range ordered {
		asset := matchHeldAsset(t.Symbol, assets)
		if asset == "" || t.Quantity <= 0 {
			continue
		}
		currency := t.Currency
		if currency == "" {
			currency = "USD"
		}
		if !strings.EqualFold(currency, "USD") {
			foreign[asset] = struct{}{}
			continue
		}
		l, ok := ledgers[asset]
		if !ok {
			l = &ledger{}
			ledgers[asset] = l
		}
		if strings.EqualFold(t.Side, "sell") {
			// A sell larger than the tracked lot means earlier acquisitions (or a
			// deposit) are missing from the history, so the basis is unknowable.
			if l.unknown || t.Quantity-l.quantity > quantityTolerance(l.quantity) {
				l.unknown = true
				l.quantity -= t.Quantity
				continue
			}
			average := l.cost / l.quantity
			l.quantity -= t.Quantity
			l.cost -= average * t.Quantity
			continue
		}
		if t.Price <= 0 {
			l.unknown = true
			l.quantity += t.Quantity
			continue
		}
		l.quantity += t.Quantity
		l.cost += t.Price * t.Quantity
	}

	out := make(map[string]float64, len(ledgers))
	for asset, l := range ledgers {
		if _, ok := foreign[asset]; ok {
			continue
		}
		if l.unknown || l.quantity <= 0 || l.cost <= 0 {
			continue
		}
		balance := held[asset]
		if math.Abs(l.quantity-balance) > quantityTolerance(balance) {
			continue
		}
		out[asset] = l.cost / l.quantity
	}
	return out
}

// matchHeldAsset resolves a trade symbol to the held balance asset it belongs to.
// Spot fills arrive as a quote pair — "BTCUSDT" or a separated "BTC-USDT" /
// "BTC/USDT" — while fiat orders carry the bare ticker ("SOL"), so the balance
// set disambiguates and the stablecoin quote is stripped.
func matchHeldAsset(symbol string, assets []string) string {
	sym := strings.ToUpper(strings.TrimSpace(symbol))
	if sym == "" {
		return ""
	}
	candidates := []string{sym}
	if normalized := strings.NewReplacer("-", "", "_", "", "/", "").Replace(sym); normalized != sym {
		candidates = append(candidates, normalized)
	}
	for _, candidate := range candidates {
		for _, asset := range assets {
			if candidate == asset {
				return asset
			}
		}
	}
	for _, candidate := range candidates {
		for _, asset := range assets {
			if strings.HasPrefix(candidate, asset) && IsStablecoin(candidate[len(asset):]) {
				return asset
			}
		}
	}
	return ""
}

// IsStablecoin returns true for the most common USD-pegged stablecoins.
func IsStablecoin(s string) bool {
	switch strings.ToUpper(s) {
	case "USDT", "USDC", "DAI", "BUSD", "TUSD", "FRAX", "USD", "USDD", "PYUSD":
		return true
	}
	return false
}
