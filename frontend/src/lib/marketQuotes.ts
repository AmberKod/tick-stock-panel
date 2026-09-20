export type MarketQuote = Record<string, unknown> & {
  symbol?: unknown
}

interface BatchQuoteResponse {
  results?: MarketQuote[]
}

const DEFAULT_BATCH_SIZE = 5000
const DEFAULT_CONCURRENCY = 1

function uniqueSymbols(symbols: string[]): string[] {
  return [...new Set(symbols.map((symbol) => symbol.trim()).filter(Boolean))]
}

async function fetchQuoteBatch(market: string, symbols: string[]): Promise<MarketQuote[]> {
  const response = await fetch(`/api/${market}/realtime/batch`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ symbols }),
  })
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`)
  const payload = await response.json() as BatchQuoteResponse
  return payload.results ?? []
}

/**
 * Fetch realtime quotes without putting the full market pool into one URL.
 * Failed chunks are ignored when another chunk succeeds, so large dashboards
 * can still render partial data during an upstream outage.
 */
export async function fetchMarketQuotes(
  market: string,
  symbols: string[],
  options: { batchSize?: number; concurrency?: number } = {},
): Promise<MarketQuote[]> {
  const deduped = uniqueSymbols(symbols)
  if (deduped.length === 0) return []

  const batchSize = Math.max(1, options.batchSize ?? DEFAULT_BATCH_SIZE)
  const concurrency = Math.max(1, options.concurrency ?? DEFAULT_CONCURRENCY)
  const batches: string[][] = []
  for (let index = 0; index < deduped.length; index += batchSize) {
    batches.push(deduped.slice(index, index + batchSize))
  }

  const results: MarketQuote[] = []
  let failedBatches = 0
  let nextBatch = 0
  const worker = async () => {
    while (nextBatch < batches.length) {
      const batch = batches[nextBatch]
      nextBatch += 1
      try {
        results.push(...await fetchQuoteBatch(market, batch))
      } catch {
        failedBatches += 1
      }
    }
  }

  await Promise.all(Array.from({ length: Math.min(concurrency, batches.length) }, worker))
  if (failedBatches === batches.length) {
    throw new Error('实时行情批量请求全部失败')
  }
  return results
}

export function quoteMapBySymbol<T extends { symbol?: unknown }>(quotes: T[]): Map<string, T> {
  return new Map(
    quotes
      .map((quote) => [String(quote.symbol ?? '').toUpperCase(), quote] as const)
      .filter(([symbol]) => symbol.length > 0),
  )
}
