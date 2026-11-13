import { Blob as NodeBlob } from 'node:buffer'
import { strToU8, zipSync } from 'fflate'
import { expect, it } from 'vitest'
import { attachmentText } from '../src/features/chat/attachment-content'

const blob = (data: string | Uint8Array) => new NodeBlob([typeof data === 'string' ? data : new Uint8Array(data)]) as unknown as Blob
const readOffice = (extension: string, files: Record<string, string>) => attachmentText(
  blob(zipSync(Object.fromEntries(Object.entries(files).map(([path, text]) => [path, strToU8(text)])))),
  `example.${extension}`, new AbortController().signal,
)

it('returns HTML as inert source and limits long text previews', async () => {
  const html = '<script>window.alert(1)</script><h1>hello</h1>'
  expect(await attachmentText(blob(html), 'page.HTML', new AbortController().signal)).toBe(html)
  const result = await attachmentText(blob('a'.repeat(200_001)), 'large.txt', new AbortController().signal)
  expect(result).toContain('预览已截断')
  expect(result.length).toBeLessThan(200_100)
})

it('extracts Word paragraphs and orders PowerPoint slides numerically', async () => {
  expect(await readOffice('docx', { 'word/document.xml': '<w:document xmlns:w="urn:word"><w:p><w:r><w:t>Hello </w:t></w:r><w:r><w:t>world</w:t></w:r></w:p><w:p><w:r><w:t>第二段</w:t></w:r></w:p></w:document>' })).toBe('Hello world\n第二段')
  const slide = (text: string) => `<sld xmlns:a="urn:ppt"><a:p><a:r><a:t>${text}</a:t></a:r></a:p></sld>`
  const result = await readOffice('pptx', { 'ppt/slides/slide10.xml': slide('last'), 'ppt/slides/slide2.xml': slide('first') })
  expect(result).toBe('第 1 页\nfirst\n\n第 2 页\nlast')
})

it('resolves sheet names, shared strings and sparse cell references without evaluating formulas', async () => {
  const result = await readOffice('xlsx', {
    'xl/workbook.xml': '<workbook xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="预算" r:id="rId1"/></sheets></workbook>',
    'xl/_rels/workbook.xml.rels': '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
    'xl/sharedStrings.xml': '<sst><si><t>费用</t></si></sst>',
    'xl/worksheets/sheet1.xml': '<worksheet><sheetData><row><c r="A1" t="s"><v>0</v></c><c r="C1"><v>42</v></c><c r="D1" t="inlineStr"><is><t>备注</t></is></c></row><row><c r="B2"><f>SUM(C1)</f><v>42</v></c></row></sheetData></worksheet>',
  })
  expect(result).toContain('预算\nA1: 费用\tC1: 42\tD1: 备注\nB2: 42')
})

it('rejects invalid Office XML and oversized decompressed content', async () => {
  await expect(readOffice('docx', { 'word/document.xml': '<broken>' })).rejects.toThrow('无法解析')
  await expect(readOffice('docx', { 'word/document.xml': 'a'.repeat(20 * 1024 * 1024 + 1) })).rejects.toThrow('过大')
})

it('does not read a cancelled preview', async () => {
  const abort = new AbortController(); abort.abort()
  await expect(attachmentText(blob('hello'), 'text.txt', abort.signal)).rejects.toThrow()
})
