/**
 * sell.js — Sell orders via TypeScript CLOB client
 * 
 * Usage: node sell.js <token_id> <price> <size> <neg_risk> [tick_size]
 * 
 * Env vars: PRIVATE_KEY, FUNDER_ADDRESS, SIGNATURE_TYPE
 */

const { ClobClient } = require("@polymarket/clob-client");
const { Wallet } = require("@ethersproject/wallet");

const HOST = "https://clob.polymarket.com";
const CHAIN_ID = 137;

async function main() {
    const args = process.argv.slice(2);
    if (args.length < 4) {
        console.log(JSON.stringify({ error: "Usage: node sell.js <token_id> <price> <size> <neg_risk> [tick_size]" }));
        process.exit(1);
    }

    const tokenId = args[0];
    const price = parseFloat(args[1]);
    const size = parseFloat(args[2]);
    const negRisk = args[3] === "true";
    const tickSize = args[4] || "0.01";

    const privateKey = process.env.PRIVATE_KEY;
    const funder = process.env.FUNDER_ADDRESS;
    const sigType = parseInt(process.env.SIGNATURE_TYPE || "2");

    if (!privateKey) {
        console.log(JSON.stringify({ error: "PRIVATE_KEY not set" }));
        process.exit(1);
    }

    try {
        const signer = new Wallet(privateKey);

        // Step 1: Create client WITH sigType and funder from the start
        // This ensures API creds are derived for the correct wallet setup
        const client = new ClobClient(
            HOST,
            CHAIN_ID,
            signer,
            undefined,  // creds will be derived
            sigType,
            funder
        );

        // Step 2: Derive API creds with correct context
        const creds = await client.createOrDeriveApiKey();

        // Step 3: Reinit with creds
        const authedClient = new ClobClient(
            HOST,
            CHAIN_ID,
            signer,
            creds,
            sigType,
            funder
        );

        // Step 4: Create sell order
        const order = await authedClient.createOrder({
            tokenID: tokenId,
            price: price,
            size: size,
            side: "SELL",
        }, {
            tickSize: tickSize,
            negRisk: negRisk,
        });

        // Step 5: Post it
        const resp = await authedClient.postOrder(order, "GTC");

        console.log(JSON.stringify({
            success: true,
            order_id: resp.orderID || "",
            status: resp.status || "",
            response: resp
        }));

    } catch (err) {
        // Output error as JSON so Python can parse it
        const errMsg = err.message || String(err);
        const errData = err.response?.data || err.data || null;
        console.log(JSON.stringify({
            error: errMsg,
            details: errData,
            status: err.response?.status || 0
        }));
        process.exit(1);
    }
}

main();
