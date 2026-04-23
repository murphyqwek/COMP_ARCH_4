(defvar running 1)
(defvar ch 0)

(def-interrupt
    (setq ch (in-char))
    (if (= ch 0)
        (setq running 0)
        (print-char ch)))

(loop i 0 1000
    (if (= running 0)
        (setq i 1000)))