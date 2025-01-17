const http = require('http');
const receiptline = require('receiptline');
const { URL } = require('url');
const { parse } = require('querystring');

// HTMLテンプレート
const generateHtml = svg => `<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Receipt</title>
<style type="text/css">
div {
    float: left;
    padding: 24px;
    box-shadow: 0 6px 12px rgba(0, 0, 0, .5);
}
</style>
</head>
<body>
<div>${svg}</div>
</body>
</html>`;

const server = http.createServer(async (req, res) => {
    if (req.method === 'POST') {
        const path = new URL(req.url, `http://${req.headers.host}`).pathname;
        if (path === '/generate') {
            let body = '';
            req.on('data', chunk => {
                body += chunk.toString();
            });
            req.on('end', async () => {
                const { text } = parse(body);
                if (text) {
                    const svg = receiptline.transform(text, { cpl: 35, encoding: 'shiftjis', spacing: true });
                    const html = generateHtml(svg);

                    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
                    res.end(html);
                } else {
                    res.writeHead(400, { 'Content-Type': 'application/json' });
                    res.end(JSON.stringify({ error: 'Invalid text data' }));
                }
            });
        } else {
            res.writeHead(404);
            res.end();
        }
    } else {
        res.writeHead(404);
        res.end();
    }
});

server.listen(6573, "0.0.0.0", () => {
    console.log('Started receipt server at: "receipt:6573/generate"');
});
