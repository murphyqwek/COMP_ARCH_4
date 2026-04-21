(defvar x 1)
(defvar a 5)

(print (setq x 13))

(setq x (if (= x 13) 100 200))
(print x) ;; Должно вывести 100

(setq x
    (loop i 1 5
        (+ i 10)
    )
)
(print x)

(print (+ (if (= a 5) 10 0) (setq a 5)))