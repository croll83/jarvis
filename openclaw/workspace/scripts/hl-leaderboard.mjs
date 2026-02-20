#!/usr/bin/env node
// Fetch Hyperliquid leaderboard and find top traders by all-time PnL

const res = await fetch('https://stats-data.hyperliquid.xyz/Mainnet/leaderboard');
const data = await res.json();

const rows = data.leaderboardRows.map(r => {
  const allTime = r.windowPerformances.find(w => w[0] === 'allTime')?.[1] || {};
  const month = r.windowPerformances.find(w => w[0] === 'month')?.[1] || {};
  const week = r.windowPerformances.find(w => w[0] === 'week')?.[1] || {};
  const day = r.windowPerformances.find(w => w[0] === 'day')?.[1] || {};
  return {
    address: r.ethAddress,
    name: r.displayName || '(anon)',
    accountValue: parseFloat(r.accountValue),
    allTimePnl: parseFloat(allTime.pnl || 0),
    allTimeRoi: parseFloat(allTime.roi || 0),
    allTimeVlm: parseFloat(allTime.vlm || 0),
    monthPnl: parseFloat(month.pnl || 0),
    monthRoi: parseFloat(month.roi || 0),
    weekPnl: parseFloat(week.pnl || 0),
    weekRoi: parseFloat(week.roi || 0),
    dayPnl: parseFloat(day.pnl || 0),
  };
});

// Sort by all-time PnL descending
rows.sort((a, b) => b.allTimePnl - a.allTimePnl);

const fmt = (n) => {
  if (Math.abs(n) >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (Math.abs(n) >= 1e3) return `$${(n / 1e3).toFixed(1)}K`;
  return `$${n.toFixed(2)}`;
};

const pct = (n) => `${(n * 100).toFixed(2)}%`;

console.log(`\nTotal leaderboard entries: ${rows.length}\n`);
console.log('=== TOP 10 BY ALL-TIME PNL ===\n');

rows.slice(0, 10).forEach((r, i) => {
  console.log(`#${i + 1} ${r.name}`);
  console.log(`   Address: ${r.address}`);
  console.log(`   Account Value: ${fmt(r.accountValue)}`);
  console.log(`   All-Time PnL: ${fmt(r.allTimePnl)} (ROI: ${pct(r.allTimeRoi)})`);
  console.log(`   Month PnL: ${fmt(r.monthPnl)} (ROI: ${pct(r.monthRoi)})`);
  console.log(`   Week PnL: ${fmt(r.weekPnl)} (ROI: ${pct(r.weekRoi)})`);
  console.log(`   Day PnL: ${fmt(r.dayPnl)}`);
  console.log(`   All-Time Volume: ${fmt(r.allTimeVlm)}`);
  console.log('');
});

// Also find top by ROI (with min account value > $100k to filter noise)
const bigAccounts = rows.filter(r => r.accountValue > 100000 && r.allTimePnl > 0);
bigAccounts.sort((a, b) => b.allTimeRoi - a.allTimeRoi);

console.log('=== TOP 5 BY ROI (account > $100K) ===\n');
bigAccounts.slice(0, 5).forEach((r, i) => {
  console.log(`#${i + 1} ${r.name}`);
  console.log(`   Address: ${r.address}`);
  console.log(`   Account Value: ${fmt(r.accountValue)}`);
  console.log(`   All-Time PnL: ${fmt(r.allTimePnl)} (ROI: ${pct(r.allTimeRoi)})`);
  console.log(`   Month PnL: ${fmt(r.monthPnl)} (ROI: ${pct(r.monthRoi)})`);
  console.log(`   Week PnL: ${fmt(r.weekPnl)} (ROI: ${pct(r.weekRoi)})`);
  console.log('');
});

// Consistency analysis: positive PnL in all windows
const consistent = rows.filter(r => r.allTimePnl > 0 && r.monthPnl > 0 && r.weekPnl > 0 && r.dayPnl > 0 && r.accountValue > 50000);
consistent.sort((a, b) => b.allTimePnl - a.allTimePnl);

console.log(`=== MOST CONSISTENT (positive PnL in ALL timeframes, account > $50K): ${consistent.length} traders ===\n`);
consistent.slice(0, 10).forEach((r, i) => {
  console.log(`#${i + 1} ${r.name}`);
  console.log(`   Address: ${r.address}`);
  console.log(`   Account Value: ${fmt(r.accountValue)}`);
  console.log(`   All-Time: ${fmt(r.allTimePnl)} (${pct(r.allTimeRoi)})`);
  console.log(`   Month: ${fmt(r.monthPnl)} (${pct(r.monthRoi)})`);
  console.log(`   Week: ${fmt(r.weekPnl)} (${pct(r.weekRoi)})`);
  console.log(`   Day: ${fmt(r.dayPnl)}`);
  console.log('');
});
