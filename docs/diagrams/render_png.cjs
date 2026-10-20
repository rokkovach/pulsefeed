// Run with the bundled Node runtime and NODE_PATH documented in README.md.
const sharp = require('sharp');
const path = require('path');
(async () => {
  for (const name of ['architecture', 'request-lifecycle', 'local-vs-cloud']) {
    const base = path.join(__dirname, name);
    await sharp(base + '.svg', { density: 144 }).png().toFile(base + '.png');
    const { width, height } = await sharp(base + '.png').metadata();
    console.log(`${name}.png: ${width} × ${height}`);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
