import { ethers } from 'ethers';
import { execSync } from 'child_process';

const provider = new ethers.JsonRpcProvider('https://rpc.hyperliquid.xyz/evm');
const pk = execSync("openssl enc -aes-256-cbc -pbkdf2 -d -in /home/jarvis/.openclaw/workspace/.secrets/hl-wallet.enc -pass pass:$(cat /etc/machine-id)").toString().trim();
const wallet = new ethers.Wallet(pk, provider);

const USDC_ADDRESS = '0xb88339CB7199b77E23DB6E890353E22632Ba630f';
const CORE_DEPOSIT_WALLET = '0x6b9e773128f453f5c2c60935ee2de2cbc5390a24';
const PERPS_DEX = 0; // deposit to perps balance

const erc20Abi = [
  'function balanceOf(address) view returns (uint256)',
  'function approve(address spender, uint256 amount) returns (bool)',
  'function allowance(address owner, address spender) view returns (uint256)',
];

const depositAbi = [
  'function deposit(uint256 amount, uint32 destinationDex) external',
];

const usdc = new ethers.Contract(USDC_ADDRESS, erc20Abi, wallet);
const depositContract = new ethers.Contract(CORE_DEPOSIT_WALLET, depositAbi, wallet);

// Check balance
const balance = await usdc.balanceOf(wallet.address);
const balanceFormatted = ethers.formatUnits(balance, 6);
console.log(`Wallet: ${wallet.address}`);
console.log(`USDC Balance on HyperEVM: ${balanceFormatted}`);

if (balance === 0n) {
  console.log('No USDC to bridge!');
  process.exit(0);
}

// Step 1: Approve CoreDepositWallet to spend USDC
console.log(`\nApproving ${balanceFormatted} USDC for CoreDepositWallet...`);
const approveTx = await usdc.approve(CORE_DEPOSIT_WALLET, balance);
console.log(`Approve TX: ${approveTx.hash}`);
await approveTx.wait();
console.log('Approved ✅');

// Step 2: Call deposit(amount, 0) for perps balance
console.log(`\nDepositing ${balanceFormatted} USDC to HyperCore Perps...`);
const depositTx = await depositContract.deposit(balance, PERPS_DEX);
console.log(`Deposit TX: ${depositTx.hash}`);
const receipt = await depositTx.wait();
console.log(`Confirmed in block: ${receipt.blockNumber} ✅`);

console.log(`\n🎉 Successfully bridged ${balanceFormatted} USDC to HyperCore Perps!`);
