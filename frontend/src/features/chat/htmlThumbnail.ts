// This is a static rendering document, never a runnable workspace page. Keep the
// sandbox on the iframe too: CSP alone is not an origin or navigation boundary.
const tags = new Set(('a abbr address article aside b bdi bdo blockquote br button caption code col colgroup ' +
  'dd del details div dl dt em fieldset figcaption figure footer form h1 h2 h3 h4 h5 h6 header hr i img input ' +
  'ins kbd label legend li main mark nav ol optgroup option p pre progress q s samp section select small ' +
  'span strong style sub summary sup table tbody td textarea tfoot th thead time tr u ul var wbr ' +
  'svg g path circle ellipse rect line polyline polygon text tspan defs lineargradient radialgradient ' +
  'stop clippath mask pattern symbol use title desc').split(' '))
const policy = "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src data:; " +
  "base-uri 'none'; form-action 'none'; frame-src 'none'; object-src 'none'"

export function htmlThumbnail(source: string): string {
  // Template contents stay inert while filtering, including resource elements.
  const template = document.createElement('template')
  template.innerHTML = source
  for (const element of template.content.querySelectorAll('*')) {
    if (!tags.has(element.localName.toLowerCase())) { element.remove(); continue }
    for (const attribute of Array.from(element.attributes)) {
      const name = attribute.name.toLowerCase()
      const value = attribute.value
      const localImage = element.localName === 'img' && name === 'src' &&
        /^data:image\/(png|jpeg|gif|webp);base64,[a-z0-9+/=\s]+$/i.test(value)
      const localUse = element.localName === 'use' && (name === 'href' || name === 'xlink:href') && /^#[\w-]+$/.test(value)
      if (name.startsWith('on') || ['src', 'srcset', 'href', 'xlink:href', 'srcdoc', 'action', 'formaction',
        'ping', 'target', 'autofocus', 'autoplay', 'background'].includes(name)) {
        if (!localImage && !localUse) element.removeAttribute(attribute.name)
      }
    }
  }
  return `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${policy}"></head><body>${template.innerHTML}</body></html>`
}
