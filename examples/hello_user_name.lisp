(defvar running 1)
(defvar ch 0)
(defvar idx 0)
(defvar buf 3000)

(def-interrupt
    (setq ch (in-char))

    (if (= ch 10)
        (setq running 0)
        (progn
            (write-mem (+ buf idx) ch)
            (setq idx (+ idx 1))
        )
    )
)

(print-pstr "What is your name?\n")

(loop i 0 20000
    (if (= running 0)
        (setq i 20000)
        (setq i i)
    )
)

(print-pstr "Hello, ")

(loop j 0 (- idx 1)
    (print-char (read-mem (+ buf j)))
)

(print-pstr "!\n")