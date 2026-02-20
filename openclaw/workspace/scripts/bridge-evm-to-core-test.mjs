import { ethers } from 'ethers';
import { execSync } from 'child_process';

const provider = new ethers.JsonRpcProvider('https://rpc.hyperliquid.xyz/evm');
const pk = execSync("openssl enc -aes-256-cbc -pbkdf2 -d -in /home/jarvis/.openclaw/workspace/.secrets/hl-wallet.enc -pass pass:$(cat /etc/machine-id)").toString().trim();
const wallet = new ethers.Wallet(pk, provider);

const USDC_ADDRESS = '0xb88339CB7199b77E23DB6E890353E22632Ba630f';
const CORE_DEPOSIT_WALLET = '0x6b9e773128f453f5c2c60935ee2de2cbc5390a24';

const amount = process.argv[2] ? ethers.parseUnits(process.argv[2], 6) : ethers.parseUnits('1', 6);
const destDex = parseInt(process.argv[3] || '0'); // 0=perps, 4294967295=spot

const erc20Abi = [
  'function balanceOf(address) view returns (uint256)',
  'function approve(address spender, uint256 amount) returns (bool)',
  'function allowance(address owner, address spender) view returns (uint256)',
];
const depositAbi = ['function deposit(uint256 amount, uint32 destinationDex) external'];

const usdc = new ethers.Contract(USDC_ADDRESS, erc20Abi, wallet);
const depositContract = new ethers.Contract(CORE_DEPOSIT_WALLET, depositAbi, wallet);

const balance = await usdc.balanceOf(wallet.address);
console.log(`Wallet: ${wallet.address}`);
console.log(`USDC on EVM: ${ethers.formatUnits(balance, 6)}`);
console.log(`Bridging: ${ethers.formatUnits(amount, 6)} USDC to HyperCore (dex=${destDex})`);

// Step 1: Approve
console.log('\n1. Approving...');
const approveTx = await usdc.approve(CORE_DEPOSIT_WALLET, amount);
console.log(`   TX: ${approveTx.hash}`);
await approveTx.wait();
console.log('   Approved ✅');

// Step 2: Deposit
console.log('2. Depositing...');
const depositTx = await depositContract.deposit(amount, destDex);
console.log(`   TX: ${depositTx.hash}`);
const receipt = await depositTx.wait();
console.log(`   Block: ${receipt.blockNumber} ✅`);

// Verify
const newBalance = await usdc.balanceOf(wallet.address);
console.log(`\nNew USDC on EVM: ${ethers.formatUnits(newBalance, 6)}`);
console.log('Done! Check HyperCore balance in a few seconds.');
