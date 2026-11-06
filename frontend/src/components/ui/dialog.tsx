import { useEffect, useRef, type ReactNode } from 'react'

/** Native <dialog>: focus trap, Esc to close and the backdrop come from the browser. */
export function Dialog({
  open,
  onClose,
  title,
  children,
}: {
  open: boolean
  onClose: () => void
  title: string
  children: ReactNode
}) {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const dialog = ref.current
    if (!dialog) return
    if (open && !dialog.open) dialog.showModal()
    if (!open && dialog.open) dialog.close()
  }, [open])
  return (
    <dialog
      ref={ref}
      onClose={onClose}
      className="m-auto w-[min(32rem,calc(100vw-2rem))] rounded-lg border border-line bg-surface p-0 text-ink backdrop:bg-black/40"
    >
      <div className="border-b border-line px-4 py-3 text-sm font-semibold">{title}</div>
      <div className="p-4">{open && children}</div>
    </dialog>
  )
}
