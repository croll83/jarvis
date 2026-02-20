import { ethers } from 'ethers';
import { execSync } from 'child_process';

const provider = new ethers.JsonRpcProvider('https://rpc.hyperliquid.xyz/evm');

// Decrypt private key
const pk = execSync("openssl enc -aes-256-cbc -pbkdf2 -d -in /home/jarvis/.openclaw/workspace/.secrets/hl-wallet.enc -pass pass:$(cat /etc/machine-id)").toString().trim();

const wallet = new ethers.Wallet(pk, provider);
console.log('Sending from:', wallet.address);

const usdcAddress = '0xb88339CB7199b77E23DB6E890353E22632Ba630f';
const to = '0xfe5b95b0f1d5e43d639d4986c4dd019be3be556e';
const amount = ethers.parseUnits('0.5', 6); // USDC has 6 decimals

const usdc = new ethers.Contract(usdcAddress, [
  'function transfer(address to, uint256 amount) returns (bool)',
  'function balanceOf(address) view returns (uint256)',
], wallet);

const bal = await usdc.balanceOf(wallet.address);
console.log('USDC balance:', ethers.formatUnits(bal, 6));

console.log(`Sending 0.5 USDC to ${to}...`);
const tx = await usdc.transfer(to, amount);
console.log('TX hash:', tx.hash);
const receipt = await tx.wait();
console.log('Confirmed in block:', receipt.blockNumber);
console.log('Done!');
