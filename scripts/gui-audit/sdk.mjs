// The app's plugin SDK, reduced to the four symbols this plugin imports.
export const PALETTE_AREA = 'palette'
export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'sidebar.nav'
export const STATUSBAR_AREAS = { left: 'statusBar.left', right: 'statusBar.right' }
export const TITLEBAR_AREAS = { left: 'titleBar.left', center: 'titleBar.center', right: 'titleBar.right' }
export const host = {
  notify: () => {},
  navigate: () => {},
  request: async () => ({}),
  state: { gateway: { get: () => 'open' } }
}
export const useValue = (atom) => (atom && typeof atom.get === 'function' ? atom.get() : atom)
