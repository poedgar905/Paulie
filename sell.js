/**
 * sell.js — Sell orders via TypeScript CLOB client
 * 
 * Usage: node sell.js <token_id> <price> <size> <neg_risk> [tick_size]
 * 
 * Example: node sell.js "abc123..." 0.50 5.0 false 0.01
 * 
 * Output: JSON with order result or error
 * 
 * Env vars needed: PRIVATE_KEY, FUNDER_ADDRESS, SIGNATURE_TYPE
 */

const { ClobClient, Side, OrderType } = require("@polymarket/clob-client");
const { Wallet } = require("@ethersproject/wallet");

const HOST = "https://clob.polymarket.com";
const CHAIN_ID = 137;

async function main() {
    const args = process.argv.slice(2);
    if (args.length < 4) {
        console.log(JSON.stringify({
            error: "Usage: node sell.js <token_id> <price> <size> <neg_risk> [tick_size]"
        }));
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

        // Init client and derive creds
        const tempClient = new ClobClient(HOST, CHAIN_ID, signer);
        const creds = await tempClient.createOrDeriveApiKey();

        const client = new ClobClient(
            HOST,
            CHAIN_ID,
            signer,
            creds,
            sigType,
            funder
        );

        // Place sell order
        const resp = await client.createAndPostOrder(
            {
                tokenID: tokenId,
                price: price,
                size: size,
                side: Side.SELL,
            },
            { tickSize: tickSize, negRisk: negRisk },
            OrderType.GTC
        );

        console.log(JSON.stringify({
            success: true,
            order_id: resp.orderID || resp.orderID || "",
            status: resp.status || "",
            response: resp
        }));

    } catch (err) {
        console.log(JSON.stringify({
            error: err.message || String(err),
            details: err.data || null
        }));
        process.exit(1);
    }
}

main();
