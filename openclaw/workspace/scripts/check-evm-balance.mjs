import { ethers } from 'ethers';

const provider = new ethers.JsonRpcProvider('https://rpc.hyperliquid.xyz/evm');
const address = '0x7b75418121134dce9D0a40a80762A8D8f379D49D';

// Native HYPE balance
const hypeBalance = await provider.getBalance(address);
console.log('HYPE:', ethers.formatEther(hypeBalance));

// Try common USDC addresses on HyperEVM
const erc20abi = ['function balanceOf(address) view returns (uint256)', 'function decimals() view returns (uint8)', 'function symbol() view returns (string)'];

const tokens = [
  '0xb88339CB7199b77E23DB6E890353E22632Ba630f',
];

for (const addr of tokens) {
  try {
    const c = new ethers.Contract(addr, erc20abi, provider);
    const [bal, dec, sym] = await Promise.all([c.balanceOf(address), c.decimals(), c.symbol()]);
    const formatted = ethers.formatUnits(bal, dec);
    if (parseFloat(formatted) > 0) console.log(`${sym} (${addr}): ${formatted}`);
  } catch(e) {}
}
