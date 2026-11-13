// Real uploads against a Lima service using config/config.json; no LLM calls.
// Creates a fresh guest and retains uploaded fixtures, matching attachment lifecycle.
// LAXAGENT_BASE_URL=http://127.0.0.1:18010 node tests/smoke-attachment-preview-lima.mjs
import { chromium, expect } from '@playwright/test'
import { existsSync, mkdirSync } from 'node:fs'
import { zipSync, strToU8 } from 'fflate'

const base = process.env.LAXAGENT_BASE_URL || 'http://127.0.0.1:8000'
const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || (existsSync('/Applications/Microsoft Edge.app') ? 'msedge' : undefined) })
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, acceptDownloads: true })
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto(base)
  const models = page.waitForResponse(response => response.url().endsWith('/api/v1/models'))
  await page.getByRole('button', { name: '以游客登录' }).click()
  const catalog = await (await models).json()
  const model = catalog.models.find(model => model.supports_multimodal)
  if (!model) throw new Error('No multimodal model configured')
  await page.getByRole('combobox', { name: '模型' }).selectOption(model.id)
  const png = await page.evaluate(() => {
    const canvas = document.createElement('canvas'); canvas.width = 240; canvas.height = 240
    const ctx = canvas.getContext('2d'), gradient = ctx.createLinearGradient(0, 0, 240, 240)
    gradient.addColorStop(0, '#49d5ec'); gradient.addColorStop(1, '#296caa')
    ctx.fillStyle = gradient; ctx.fillRect(0, 0, 240, 240)
    ctx.fillStyle = '#ffe2a2'; ctx.beginPath(); ctx.arc(165, 70, 28, 0, Math.PI * 2); ctx.fill()
    return canvas.toDataURL().split(',')[1]
  })
  const zip = files => Buffer.from(zipSync(Object.fromEntries(Object.entries({ '[Content_Types].xml': '<Types/>', ...files }).map(([path, value]) => [path, strToU8(value)]))))
  const stream = 'BT /F1 18 Tf 20 150 Td (PDF preview content) Tj ET'
  const pdf = `%PDF-1.4\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj\n4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n5 0 obj << /Length ${stream.length} >> stream\n${stream}\nendstream\nendobj\ntrailer << /Root 1 0 R >>\n%%EOF`
  const samples = [
    { name: '单行文件名展示测试_很长的项目介绍文档.pdf', buffer: Buffer.from(pdf), expected: /PDF preview content/ },
    { name: '页面.html', buffer: Buffer.from('<h1>页面源码</h1><script>document.body.dataset.compromised="yes"</script>'), expected: /<script>/ },
    { name: '预算.xlsx', buffer: zip({
      'xl/workbook.xml': '<workbook xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="预算" r:id="rId1"/></sheets></workbook>',
      'xl/_rels/workbook.xml.rels': '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
      'xl/worksheets/sheet1.xml': '<worksheet><sheetData><row><c r="A1" t="inlineStr"><is><t>总计</t></is></c><c r="B1"><v>128</v></c></row></sheetData></worksheet>',
    }), expected: /A1: 总计\tB1: 128/ },
    { name: '笔记.docx', buffer: zip({ 'word/document.xml': '<w:document xmlns:w="urn:word"><w:p><w:r><w:t>文档内容预览</w:t></w:r></w:p></w:document>' }), expected: /文档内容预览/ },
  ]
  await page.getByLabel('选择图片').setInputFiles({ name: '图片.png', mimeType: 'image/png', buffer: Buffer.from(png, 'base64') })
  await page.getByLabel('选择文件').setInputFiles(samples.map(({ name, buffer }) => ({ name, buffer, mimeType: 'application/octet-stream' })))
  await expect(page.getByText('已上传')).toHaveCount(5, { timeout: 20000 })
  await page.getByRole('button', { name: '查看 图片.png' }).click()
  await expect(page.getByRole('dialog').getByRole('img')).toBeVisible()
  let download = page.waitForEvent('download')
  await page.getByRole('button', { name: '下载 图片.png' }).click()
  expect((await download).suggestedFilename()).toBe('图片.png')
  await page.getByRole('button', { name: '关闭图片预览' }).click()
  for (const sample of samples) {
    await page.getByRole('button', { name: `查看 ${sample.name}` }).click()
    await expect(page.getByRole('textbox', { name: `${sample.name} 文件内容` })).toHaveValue(sample.expected, { timeout: 20000 })
    download = page.waitForEvent('download')
    await page.getByRole('button', { name: `下载 ${sample.name}` }).click()
    expect((await download).suggestedFilename()).toBe(sample.name)
    await page.getByRole('button', { name: '关闭文件预览' }).click()
  }
  expect(await page.locator('body').getAttribute('data-compromised')).toBeNull()
  await page.getByRole('button', { name: '查看 图片.png' }).scrollIntoViewIfNeeded()
  mkdirSync('test-results', { recursive: true })
  await page.screenshot({ path: 'test-results/lima-attachment-cards.png' })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('button', { name: `查看 ${samples[0].name}` }).click()
  await expect(page.getByRole('textbox', { name: `${samples[0].name} 文件内容` })).toHaveValue(samples[0].expected)
  await page.screenshot({ path: 'test-results/lima-attachment-preview-mobile.png' })
  await page.getByRole('button', { name: '关闭文件预览' }).click()
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false)
  if (errors.length) throw new Error(errors.join('\n'))
  console.log('PASS Lima: real image/PDF/HTML/XLSX/DOCX uploads, local content previews, downloads, mobile layout; no LLM calls')
} finally { await browser.close() }
