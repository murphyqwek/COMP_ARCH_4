(defvar running 1)
(defvar ch 0)

(def-interrupt
    (setq ch (in-char))
    (if (= ch 0)
        (setq running 0)
        (print-char ch)
    )
)

; Основной цикл ждет прерываний
(loop i 0 1000
    (if (= running 0)
        (setq i 1000) ; Выход из цикла
        (setq running running) ; NOP
    )
)