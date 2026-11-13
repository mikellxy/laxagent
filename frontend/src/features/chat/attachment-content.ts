// Preview extraction is local to the browser; model inputs keep the original file.
const MAX_TEXT = 200_000
const MAX_XML_BYTES = 20 * 1024 * 1024
const TRUNCATED = '\n\n（预览已截断，下载可查看完整文件）'
export const extensionOf = (name: string) => name.split('.').pop()?.toLowerCase() ?? ''
const clipped = (text: string) => text.length > MAX_TEXT ? text.slice(0, MAX_TEXT) + TRUNCATED : text

function elements(root: Document | Element, name: string) {
  return Array.from(root.getElementsByTagNameNS('*', name))
}
const texts = (root: Document | Element) => elements(root, 't').map(node => node.textContent ?? '').join('')

async function officeText(data: Uint8Array, extension: string) {
  const { unzipSync } = await import('fflate')
  let expanded = 0
  const files = unzipSync(data, { filter: file => {
    const needed = extension === 'docx' ? file.name === 'word/document.xml'
      : extension === 'pptx' ? /^ppt\/slides\/slide\d+\.xml$/.test(file.name)
      : /^xl\/(workbook\.xml|_rels\/workbook\.xml\.rels|sharedStrings\.xml|worksheets\/[^/]+\.xml)$/.test(file.name)
    if (!needed) return false
    expanded += file.originalSize
    if (expanded > MAX_XML_BYTES) throw new Error('文件内容过大，请下载查看完整文件。')
    return true
  } })
  const xml = (path: string) => {
    if (!files[path]) throw new Error('文件内容不完整，请下载查看原件。')
    const doc = new DOMParser().parseFromString(new TextDecoder().decode(files[path]), 'application/xml')
    if (doc.getElementsByTagName('parsererror').length) throw new Error('文件内容无法解析，请下载查看原件。')
    return doc
  }
  if (extension === 'docx') return clipped(elements(xml('word/document.xml'), 'p').map(texts).join('\n'))
  if (extension === 'pptx') {
    const slides = Object.keys(files).sort((a, b) => a.localeCompare(b, undefined, { numeric: true }))
    return clipped(slides.map((path, i) => `第 ${i + 1} 页\n${elements(xml(path), 'p').map(texts).join('\n')}`).join('\n\n'))
  }
  const strings = files['xl/sharedStrings.xml'] ? elements(xml('xl/sharedStrings.xml'), 'si').map(texts) : []
  const relations = new Map(elements(xml('xl/_rels/workbook.xml.rels'), 'Relationship').map(rel => [rel.getAttribute('Id'), rel.getAttribute('Target') ?? '']))
  let result = ''
  for (const sheet of elements(xml('xl/workbook.xml'), 'sheet')) {
    const id = sheet.getAttributeNS('http://schemas.openxmlformats.org/officeDocument/2006/relationships', 'id')
    const target = relations.get(id)
    if (!target) throw new Error('无法读取工作表，请下载查看原件。')
    const path = new URL(target, 'https://office.invalid/xl/').pathname.slice(1)
    result += `${sheet.getAttribute('name') ?? '工作表'}\n`
    for (const row of elements(xml(path), 'row')) {
      // Keep cell references so sparse sheets cannot shift values into the wrong column.
      result += elements(row, 'c').map(cell => {
        const type = cell.getAttribute('t'), raw = elements(cell, 'v')[0]?.textContent ?? ''
        const formula = elements(cell, 'f')[0]?.textContent
        const value = type === 's' ? strings[Number(raw)] ?? '' : type === 'inlineStr' ? texts(cell)
          : type === 'b' ? (raw === '1' ? 'TRUE' : 'FALSE') : raw || (formula ? `=${formula}` : '')
        return `${cell.getAttribute('r') ?? ''}: ${value}`
      }).join('\t') + '\n'
      if (result.length > MAX_TEXT) return clipped(result)
    }
    result += '\n'
  }
  return clipped(result)
}

async function pdfText(data: Uint8Array, signal: AbortSignal) {
  const [pdfjs, worker] = await Promise.all([import('pdfjs-dist'), import('pdfjs-dist/build/pdf.worker.min.mjs?url')])
  signal.throwIfAborted()
  pdfjs.GlobalWorkerOptions.workerSrc = worker.default
  const task = pdfjs.getDocument({ data, useSystemFonts: true })
  const abort = () => { void task.destroy() }
  signal.addEventListener('abort', abort, { once: true })
  try {
    const doc = await task.promise
    let result = '', hasText = false
    for (let i = 1; i <= Math.min(doc.numPages, 200); i++) {
      signal.throwIfAborted()
      const page = await doc.getPage(i), content = await page.getTextContent()
      const text = content.items.map(item => 'str' in item ? item.str + (item.hasEOL ? '\n' : ' ') : '').join('')
      hasText ||= !!text.trim()
      result += `第 ${i} 页\n${text}\n\n`
      page.cleanup()
      if (result.length > MAX_TEXT) return clipped(result)
    }
    if (!hasText) return '此 PDF 没有可提取的文本，可能为扫描件。请下载查看原件。'
    return result + (doc.numPages > 200 ? TRUNCATED : '')
  } finally {
    signal.removeEventListener('abort', abort)
    await task.destroy()
  }
}

export async function attachmentText(blob: Blob, name: string, signal: AbortSignal): Promise<string> {
  const extension = extensionOf(name)
  signal.throwIfAborted()
  if (['txt', 'md', 'csv', 'html', 'htm'].includes(extension)) {
    const text = await blob.slice(0, MAX_TEXT * 4).text()
    signal.throwIfAborted()
    return clipped(text) || '文件内容为空。'
  }
  const data = new Uint8Array(await blob.arrayBuffer())
  signal.throwIfAborted()
  const text = extension === 'pdf' ? await pdfText(data, signal)
    : ['docx', 'xlsx', 'pptx'].includes(extension) ? await officeText(data, extension)
    : '暂不支持此格式的内容预览，请下载查看。'
  signal.throwIfAborted()
  return text.trim() || '文件中没有可预览的文本，请下载查看原件。'
}
