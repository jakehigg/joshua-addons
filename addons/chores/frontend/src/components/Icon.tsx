// Stroke icons in the style of the shared Joshua set. `.j-icon` in
// joshua-ui.css gives the size and the stroke.
const ICON_PATHS = {
  plus: 'M12 5v14M5 12h14',
  minus: 'M5 12h14',
  edit: 'M4 20h4L19 9l-4-4L4 16z',
  trash: 'M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13',
  check: 'm5 12.5 4.5 4.5L19 7.5',
  close: 'M6 6l12 12M18 6 6 18',
  up: 'M12 19V5M6 11l6-6 6 6',
  down: 'M12 5v14M6 13l6 6 6-6',
  left: 'M15 6l-6 6 6 6',
  right: 'M9 6l6 6-6 6',
  signout: 'M14 4h5v16h-5M10 8l-4 4 4 4M6 12h10',
} as const

export type IconName = keyof typeof ICON_PATHS

export default function Icon({ name }: { name: IconName }) {
  return (
    <svg className="j-icon" viewBox="0 0 24 24" aria-hidden="true">
      <path d={ICON_PATHS[name]} />
    </svg>
  )
}
